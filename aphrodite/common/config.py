from typing import Optional

import torch
from transformers import PretrainedConfig
from transformers.utils.quantization_config import QuantizationMethod

from aphrodite.common.logger import init_logger
from aphrodite.transformers_utils.config import get_config
from aphrodite.common.utils import get_cpu_memory

logger = init_logger(__name__)

_GB = 1 << 30


class ModelConfig:
    """Configuration for the model.

    Args:
        model: Name or path of the huggingface model to use.
        tokenizer: Name or path of the huggingface tokenizer to use.
        tokenizer_mode: Tokenizer mode. "auto" will use the fast tokenizer if
            available, and "slow" will always use the slow tokenizer.
        trust_remote_code: Trust remote code (e.g., from HuggingFace) when
            downloading the model and tokenizer.
        download_dir: Directory to download and load the weights, default to the
            default cache directory of huggingface.
        load_format: The format of the model weights to load:
            "auto" will try to load the weights in the safetensors format and
                fall back to the pytorch bin format if safetensors format is
                not available.
            "pt" will load the weights in the pytorch bin format.
            "safetensors" will load the weights in the safetensors format.
            "npcache" will load the weights in pytorch format and store
                a numpy cache to speed up the loading.
            "dummy" will initialize the weights with random values, which is
                mainly for profiling.
        dtype: Data type for model weights and activations. The "auto" option
            will use FP16 precision for FP32 and FP16 models, and BF16 precision
            for BF16 models.
        seed: Random seed for reproducibility.
        revision: The specific model version to use. It can be a branch name,
            a tag name, or a commit id. If unspecified, will use the default
            version.
        max_model_len: Maximum length of a sequence (including prompt and
            output). If None, will be derived from the model.
        quantization: Quantization method that was used to quantize the model
            weights. If None, we assume the model weights are not quantized.
    """

    def __init__(
        self,
        model: str,
        tokenizer: str,
        tokenizer_mode: str,
        trust_remote_code: bool,
        download_dir: Optional[str],
        load_format: str,
        dtype: str,
        seed: int,
        revision: Optional[str] = None,
        max_model_len: Optional[int] = None,
        quantization: Optional[str] = None,
    ) -> None:
        self.model = model
        self.tokenizer = tokenizer
        self.tokenizer_mode = tokenizer_mode
        self.trust_remote_code = trust_remote_code
        self.download_dir = download_dir
        self.load_format = load_format
        self.seed = seed
        self.revision = revision
        self.quantization = quantization

        self.hf_config = get_config(model, trust_remote_code, revision)
        self.dtype = _get_and_verify_dtype(self.hf_config, dtype)
        self.max_model_len = _get_and_verify_max_len(self.hf_config,
                                                     max_model_len)
        self._verify_load_format()
        self._verify_tokenizer_mode()
        self._verify_quantization()

    def _verify_load_format(self) -> None:
        load_format = self.load_format.lower()
        if load_format not in [
                "auto", "pt", "safetensors", "npcache", "dummy"
        ]:
            raise ValueError(
                f"Unknown load format: {self.load_format}. Must be one of "
                "'auto', 'pt', 'safetensors', 'npcache', or 'dummy'.")
        self.load_format = load_format

    def _verify_tokenizer_mode(self) -> None:
        tokenizer_mode = self.tokenizer_mode.lower()
        if tokenizer_mode not in ["auto", "slow"]:
            raise ValueError(
                f"Unknown tokenizer mode: {self.tokenizer_mode}. Must be "
                "either 'auto' or 'slow'.")
        self.tokenizer_mode = tokenizer_mode

    def _verify_quantization(self) -> None:
        supported_quantization = ["awq", "gptq"]
        if hasattr(self.hf_config, "quantization_config"
                   ) and self.hf_config.quantization_config.get(
                       "quant_method") == QuantizationMethod.GPTQ:
            self.quantization = "gptq"
        if self.quantization is None:
            return
        quantization = self.quantization.lower()
        if quantization not in supported_quantization:
            raise ValueError(
                f"Unknown quantization: {self.quantization}. Must be one of "
                f"{supported_quantization}.")
        self.quantization = quantization

    def verify_with_parallel_config(
        self,
        parallel_config: "ParallelConfig",
    ) -> None:
        total_num_attention_heads = self.hf_config.num_attention_heads
        tensor_parallel_size = parallel_config.tensor_parallel_size
        if total_num_attention_heads % tensor_parallel_size != 0:
            raise ValueError(
                f"Total number of attention heads ({total_num_attention_heads})"
                " must be divisible by tensor parallel size "
                f"({tensor_parallel_size}).")

        total_num_hidden_layers = self.hf_config.num_hidden_layers
        pipeline_parallel_size = parallel_config.pipeline_parallel_size
        if total_num_hidden_layers % pipeline_parallel_size != 0:
            raise ValueError(
                f"Total number of hidden layers ({total_num_hidden_layers}) "
                "must be divisible by pipeline parallel size "
                f"({pipeline_parallel_size}).")

    def get_hidden_size(self) -> int:
        return self.hf_config.hidden_size

    def get_head_size(self) -> int:
        # FIXME(woosuk): This may not be true for all models.
        return self.hf_config.hidden_size // self.hf_config.num_attention_heads

    def get_num_kv_heads(self, parallel_config: "ParallelConfig") -> int:
        """Returns the number of KV heads per GPU worker."""
        if getattr(self.hf_config, "n_head_kv", None) is not None:
            return (self.hf_config.n_head_kv //
                    parallel_config.tensor_parallel_size)
        if getattr(self.hf_config, "num_kv_heads", None) is not None:
            return (self.hf_config.num_kv_heads //
                    parallel_config.tensor_parallel_size)
        # For LLaMA-2:
        if getattr(self.hf_config, "num_key_value_heads", None) is not None:
            return (self.hf_config.num_key_value_heads //
                    parallel_config.tensor_parallel_size)
        total_num_attention_heads = self.hf_config.num_attention_heads
        return total_num_attention_heads // parallel_config.tensor_parallel_size

    def get_max_model_len(self) -> int:
        return self.max_model_len

    def get_num_layers(self, parallel_config: "ParallelConfig") -> int:
        total_num_hidden_layers = self.hf_config.num_hidden_layers
        return total_num_hidden_layers // parallel_config.pipeline_parallel_size


class CacheConfig:
    """Configuration for the KV cache.

    Args:
        block_size: Size of a cache block in number of tokens.
        gpu_memory_utilization: Fraction of GPU memory to use for the
            Aphrodite execution.
        swap_space: Size of the CPU swap space per GPU (in GiB).
    """

    def __init__(
        self,
        block_size: int,
        gpu_memory_utilization: float,
        swap_space: int,
        sliding_window: Optional[int] = None,
    ) -> None:
        self.block_size = block_size
        self.gpu_memory_utilization = gpu_memory_utilization
        self.swap_space_bytes = swap_space * _GB
        self.sliding_window = sliding_window
        self._verify_args()

        # Will be set after profiling.
        self.num_gpu_blocks = None
        self.num_cpu_blocks = None

    def _verify_args(self) -> None:
        if self.gpu_memory_utilization > 1.0:
            raise ValueError(
                "GPU memory utilization must be less than 1.0. Got "
                f"{self.gpu_memory_utilization}.")

    def verify_with_parallel_config(
        self,
        parallel_config: "ParallelConfig",
    ) -> None:
        total_cpu_memory = get_cpu_memory()
        # FIXME: Here, it is assumed that the GPUs in a tensor parallel
        # group are in the same node. However, the GPUs may span multiple nodes.
        num_gpus_per_node = parallel_config.tensor_parallel_size
        cpu_memory_usage = self.swap_space_bytes * num_gpus_per_node

        msg = (f"{cpu_memory_usage / _GB:.2f} GiB out of "
               f"the {total_cpu_memory / _GB:.2f} GiB total CPU memory is "
               "allocated for the swap space.")
        if cpu_memory_usage > 0.7 * total_cpu_memory:
            raise ValueError("Too large swap space. " + msg)
        elif cpu_memory_usage > 0.4 * total_cpu_memory:
            logger.warning("Possibly too large swap space. " + msg)


class ParallelConfig:
    """Configuration for the distributed execution.

    Args:
        pipeline_parallel_size: Number of pipeline parallel groups.
        tensor_parallel_size: Number of tensor parallel groups.
        worker_use_ray: Whether to use Ray for model workers. Will be set to
            True if either pipeline_parallel_size or tensor_parallel_size is
            greater than 1.
    """

    def __init__(
        self,
        pipeline_parallel_size: int,
        tensor_parallel_size: int,
        worker_use_ray: bool,
    ) -> None:
        self.pipeline_parallel_size = pipeline_parallel_size
        self.tensor_parallel_size = tensor_parallel_size
        self.worker_use_ray = worker_use_ray

        self.world_size = pipeline_parallel_size * tensor_parallel_size
        if self.world_size > 1:
            self.worker_use_ray = True
        self._verify_args()

    def _verify_args(self) -> None:
        if self.pipeline_parallel_size > 1:
            raise NotImplementedError(
                "Pipeline parallelism is not supported yet.")


class SchedulerConfig:
    """Scheduler configuration.

    Args:
        max_num_batched_tokens: Maximum number of tokens to be processed in
            a single iteration.
        max_num_seqs: Maximum number of sequences to be processed in a single
            iteration.
        max_model_len: Maximum length of a sequence (including prompt
            and generated text).
    """

    def __init__(
            self,
            max_num_batched_tokens: Optional[int],
            max_num_seqs: int,
            max_model_len: int,
    ) -> None:
        if max_num_batched_tokens is not None:
            self.max_num_batched_tokens = max_num_batched_tokens
        else:
            self.max_num_batched_tokens = max(max_model_len, 2048)
        self.max_num_seqs = max_num_seqs
        self.max_model_len = max_model_len
        self._verify_args()

    def _verify_args(self) -> None:
        if self.max_num_batched_tokens < self.max_model_len:
            raise ValueError(
                f"max_num_batched_tokens ({self.max_num_batched_tokens}) is "
                f"smaller than max_model_len ({self.max_model_len}). "
                f"This effectively limits the maximum sequence length to "
                f"max_num_batched_tokens and makes Aphrodite reject longer "
                f"sequences. Please increase max_num_batched_tokens or "
                f"decrease max_model_len.")
        if self.max_num_batched_tokens < self.max_num_seqs:
            raise ValueError(
                f"max_num_batched_tokens ({self.max_num_batched_tokens}) must "
                f"be greater than or equal to max_num_seqs ({self.max_num_seqs}).")


_STR_DTYPE_TO_TORCH_DTYPE = {
    "half": torch.float16,
    "float16": torch.float16,
    "float": torch.float32,
    "float32": torch.float32,
    "bfloat16": torch.bfloat16,
}


def _get_and_verify_dtype(
    config: PretrainedConfig,
    dtype: str,
) -> torch.dtype:
    # NOTE: getattr(config, "torch_dtype", torch.float32) is not correct
    # because config.torch_dtype can be None.
    config_dtype = getattr(config, "torch_dtype", None)
    if config_dtype is None:
        config_dtype = torch.float32

    dtype = dtype.lower()
    if dtype == "auto":
        if config_dtype == torch.float32:
            # Following the common practice, we use float16 for float32 models.
            torch_dtype = torch.float16
        else:
            torch_dtype = config_dtype
    else:
        if dtype not in _STR_DTYPE_TO_TORCH_DTYPE:
            raise ValueError(f"Unknown dtype: {dtype}")
        torch_dtype = _STR_DTYPE_TO_TORCH_DTYPE[dtype]

    # Verify the dtype.
    if torch_dtype != config_dtype:
        if torch_dtype == torch.float32:
            # Upcasting to float32 is allowed.
            pass
        elif config_dtype == torch.float32:
            # Downcasting from float32 to float16 or bfloat16 is allowed.
            pass
        else:
            # Casting between float16 and bfloat16 is allowed with a warning.
            logger.warning(f"Casting {config_dtype} to {torch_dtype}.")

    return torch_dtype


def _get_and_verify_max_len(
    hf_config: PretrainedConfig,
    max_model_len: Optional[int],
) -> int:
    """Get and verify the model's maximum length."""
    derived_max_model_len = float("inf")
    possible_keys = [
        "max_position_embeddings",
        "n_positions",
        "max_seq_len",
        "max_sequence_length",
        "max_seq_length",
        "seq_len",
    ]
    for key in possible_keys:
        max_len_key = getattr(hf_config, key, None)
        if max_len_key is not None:
            derived_max_model_len = min(derived_max_model_len, max_len_key)
    if derived_max_model_len == float('inf'):
        raise ValueError(
            "The model's config.json must contain one of the following keys "
            f"to determine the original maximum length of the model: {possible_keys}")

    rope_scaling = getattr(hf_config, "rope_scaling", None)
    if rope_scaling is not None:
        assert "factor" in rope_scaling
        scaling_factor = rope_scaling["factor"]
        derived_max_model_len *= scaling_factor

    if max_model_len is None:
        max_model_len = derived_max_model_len
    elif max_model_len > derived_max_model_len:
        raise ValueError(
            f"User-specified max_model_len ({max_model_len}) is greater than "
            f"the derived max_model_len ({max_len_key}={derived_max_model_len}"
            " in model's config.json). This may lead to incorrect model "
            "outputs or CUDA errors. Make sure the value is correct and "
            "within the model context size.")
    return int(max_model_len)
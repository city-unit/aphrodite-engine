from typing import Optional

import torch
from torch.nn.parameter import Parameter

from aphrodite import quantization_ops
from aphrodite.modeling.megatron.layers import (
    ColumnParallelLinear, RowParallelLinear)


class SqueezeLLMColumnParallelLinear(ColumnParallelLinear):

    def create_weights(self, dtype: torch.dtype) -> None:
        assert self.input_size % self.quant_config.weight_bits == 0
        assert (self.output_size_per_partition %
                self.quant_config.pack_factor == 0)
        self.qweight = Parameter(
            torch.empty(
                self.input_size // self.quant_config.pack_factor,
                self.output_size_per_partition,
                device="cuda",
                dtype=torch.int32,
            ),
            requires_grad=False,
        )
        self.lookup_table = Parameter(
            torch.empty(
                self.output_size_per_partition,
                self.quant_config.weight_bits**2,
                device="cuda",
                dtype=dtype,
            ),
            requires_grad=False,
        )

    def apply_weights(self, x: torch.Tensor, bias: Optional[torch.Tensor]) -> torch.Tensor:
        out_shape = (x.shape[-2], self.qweight.shape[-1])
        reshaped_x = x.reshape(-1, x.shape[-1])
        out = torch.zeros(out_shape, device="cuda", dtype=torch.float16)
        quantization_ops.squeezellm_gemm(reshaped_x, self.qweight, out, self.lookup_table)

        if bias is not None:
            out = out + bias
        return out.reshape(out_shape)
    

class SqueezeLLMRowParallelLinear(RowParallelLinear):

    def create_weights(self, dtype: torch.dtype) -> None:
        assert (self.input_size_per_partition % self.quant_config.weight_bits == 0)
        assert self.output_size % self.quant_config.pack_factor == 0
        self.qweight = Parameter(
            torch.empty(
                self.input_size_per_partition // self.quant_config.pack_factor,
                self.output_size,
                device="cuda",
                dtype=torch.int32,
            ),
            requires_grad=False,
        )
        self.lookup_table = Parameter(
            torch.empty(
                self.output_size,
                self.quant_config.weight_bits**2,
                device="cuda",
                dtype=dtype,
            ),
            requires_grad=False,
        )

    def apply_weights(self, x: torch.Tensor) -> torch.Tensor:
        reshaped_x = x.reshape(-1, x.shape[-1])
        out_shape = (x.shape[-2], self.qweight.shape[-1])
        out = torch.zeros(out_shape, device="cuda", dtype=torch.float16)
        quantization_ops.squeezellm_gemm(reshaped_x, self.qweight, out, self.lookup_table)
        return out.reshape(out_shape)
    
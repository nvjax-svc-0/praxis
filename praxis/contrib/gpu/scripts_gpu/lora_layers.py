# coding=utf-8
# Copyright 2022 The Pax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import string
import math
from jax import numpy as jnp
from jax.ad_checkpoint import checkpoint_name

from praxis import base_layer
from praxis import pax_fiddle
from praxis import pytypes
from praxis.layers.attentions import AttentionProjection, CombinedQKVProjectionLayer
from praxis.layers.linears import Linear


WeightInit = base_layer.WeightInit
LayerTpl = pax_fiddle.Config[base_layer.BaseLayer]
template_field = base_layer.template_field
WeightHParams = base_layer.WeightHParams
JTensor = pytypes.JTensor


class LoraTheta(base_layer.Theta):
    def __init__(self, module):
        self.module = module

    def _lora_initialized(self):
        if (
            self.module.has_variable("params", "lora_a")
            and self.module.has_variable("params", "lora_b")
            and "lora_a" in self.module._weight_hparams
            and "lora_b" in self.module._weight_hparams
        ):
            return True
        else:
            return False

    def _lorafy_var(self, var):
        lora_a = super().__getattr__("lora_a")
        lora_b = super().__getattr__("lora_b")
        new_var = self.module.einsum("...dr,...nr->...dn", lora_a, lora_b)
        new_var = jnp.reshape(new_var, var.shape)
        new_var += var
        return new_var

    def __getattr__(self, k):
        var = super().__getattr__(k)
        if not self._lora_initialized():
            return var

        if k == "w":
            return self._lorafy_var(var)

        return var

    def __getitem__(self, k):
        var = super().__getattr__(k)
        if not self._lora_initialized():
            return var

        if k == "w":
            return self._lorafy_var(var)

        return var


class LoraThetaDescriptor:
    """Dot syntax accession descriptor."""

    def __get__(self, obj, objtype=None):
        return LoraTheta(obj)


class LoraLinear(Linear):
    rank: int = 0
    lora_init: WeightInit | None = None
    theta = LoraThetaDescriptor()

    def setup(self) -> None:
        lora_init = self.lora_init if self.lora_init else self.weight_init

        super().setup()
        self.create_variable(
            "lora_a",
            WeightHParams(
                shape=[self.input_dims, self.rank],
                init=lora_init,
                mesh_shape=self.mesh_shape,
                tensor_split_dims_mapping=[None, None],
            ),
        )
        self.create_variable(
            "lora_b",
            WeightHParams(
                shape=[self.output_dims, self.rank],
                init=WeightInit.Constant(scale=0.0),
                mesh_shape=self.mesh_shape,
                tensor_split_dims_mapping=[None, None],
            ),
        )


class LoraAttentionProjection(AttentionProjection):
    rank: int = 0
    lora_init: WeightInit | None = None
    theta = LoraThetaDescriptor()

    def setup(self) -> None:
        super().setup()
        w_weight_params = self._weight_hparams["w"]
        lora_init = self.lora_init if self.lora_init else w_weight_params.init

        self.create_variable(
            "lora_a",
            WeightHParams(
                shape=[self.input_dim, self.rank],
                init=lora_init,
                mesh_shape=self.mesh_shape,
                tensor_split_dims_mapping=[
                    None,
                    None,
                ],
            ),
        )
        self.create_variable(
            "lora_b",
            WeightHParams(
                shape=[self.dim_per_head * self.num_heads, self.rank],
                init=WeightInit.Constant(scale=0.0),
                mesh_shape=self.mesh_shape,
                tensor_split_dims_mapping=[
                    None,
                    None,
                ],
            ),
        )


class LoraCombinedQKVProjection(CombinedQKVProjectionLayer):
    rank: int = 0
    lora_init: WeightInit | None = None
    theta = LoraThetaDescriptor()

    def setup(self) -> None:
        super().setup()
        w_weight_params = self._weight_hparams["w"]
        lora_init = self.lora_init if self.lora_init else w_weight_params.init

        self.create_variable(
            "lora_a",
            WeightHParams(
                shape=[3, self.input_dim, self.rank],
                init=lora_init,
                mesh_shape=self.mesh_shape,
                tensor_split_dims_mapping=[None, None, None],
            ),
        )
        self.create_variable(
            "lora_b",
            WeightHParams(
                shape=[3, self.input_dim, self.rank],
                init=WeightInit.Constant(scale=0.0),
                mesh_shape=self.mesh_shape,
                tensor_split_dims_mapping=[None, None, None],
            ),
        )

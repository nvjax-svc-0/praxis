# coding=utf-8
# Copyright 2022 Google LLC.
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

"""Tests for Praxis streaming normalization layers."""

from absl.testing import absltest
from absl.testing import parameterized
import numpy as np
from praxis import base_layer
from praxis.layers import streaming
from praxis.layers.streaming import test_utils

instantiate = base_layer.instantiate


class StreamingNormalizationTest(test_utils.StreamingTest):

  @parameterized.named_parameters(
      ('Basic',),
      ('Group4Dim2', 1, 4, 2),
      ('Group1', 1, 1),
      ('Stride2', 2),
      ('Stride2Group1', 2, 1),
      ('Stride4', 4),
  )
  def test_stream(self, stride=1, num_groups=2, dim=4):
    seqlen = 8
    batch, input_dim = 2, dim
    np.random.seed(None)
    inputs = np.random.normal(
        0.1, 0.5, [batch, seqlen, 1, input_dim]).astype(np.float32)
    paddings = np.array([[1, 1, 1, 0, 0, 0, 1, 1]]).astype(np.float32)
    paddings = np.concatenate((paddings, paddings), axis=0)

    context_p = base_layer.JaxContext.HParams(do_eval=True)
    with base_layer.JaxContext.new_context(hparams=context_p):
      p = streaming.GroupNorm.HParams(
          name='stream_gn',
          dim=input_dim,
          num_groups=num_groups,
          cumulative=True,
          input_rank=4)

    self.assertEqual(p.cls.get_stride(p), 1)
    self.assertEqual(p.cls.get_right_context(p), 0)
    self._compare_stream_non_stream(
        inputs, paddings, p, p, stride)


if __name__ == '__main__':
  absltest.main()

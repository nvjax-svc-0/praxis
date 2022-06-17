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

"""Attention layers."""

import functools
import string
from typing import Callable, Dict, Optional, Sequence, Tuple, Union

from flax import linen as nn
import jax
from jax import numpy as jnp
from jax.ad_checkpoint import checkpoint_name
import numpy as np
from praxis import asserts
from praxis import base_layer
from praxis import py_utils
from praxis import pytypes
from praxis.layers import embedding_softmax
from praxis.layers import stochastics

NestedMap = py_utils.NestedMap
WeightInit = base_layer.WeightInit
WeightHParams = base_layer.WeightHParams
sub_config_field = base_layer.sub_config_field
JTensor = pytypes.JTensor
NestedJTensor = pytypes.NestedJTensor
NestedInt = pytypes.NestedInt

SplitDimsMapping = pytypes.SplitDimsMapping
BaseHParams = base_layer.BaseLayer.HParams
BaseWtShardingHParams = base_layer.BaseLayer.WeightShardingHParams
BaseActShardingHParams = base_layer.BaseLayer.ActivationShardingHParams

PREFIX_DECODE_CACHE = base_layer.PREFIX_DECODE_CACHE


def limited_context_mask_from_padding(
    paddings: JTensor,
    left_context: int,
    right_context: int,
    dtype: jnp.dtype = jnp.float32) -> JTensor:
  """Generates a logit mask from padding and window configuration.

  left_context includes the current timestep and left
  ones while right_context includes only future timesteps. None represents
  infinity.

  Args:
    paddings: binary JTensor of shape [B, T], with 1 denoting padding token.
    left_context: integer or None.
    right_context: integer or None
    dtype: data type of the output.

  Returns:
    A JTensor of shape [B, 1, T, T] ready to add to attention logits.
  """
  padding_mask = convert_paddings_to_mask(paddings)
  rev_padding_mask = jnp.transpose(padding_mask, (0, 1, 3, 2))
  large_negative_number = py_utils.get_large_negative_number(dtype)
  t = paddings.shape[1]

  if right_context is None:
    right_context = t
  if left_context is None:
    left_context = t
  col_idx = jnp.tile(jnp.arange(t)[jnp.newaxis, :], [t, 1])
  row_idx = jnp.tile(jnp.arange(t)[:, jnp.newaxis], [1, t])
  mask = (
      (col_idx + left_context <= row_idx) |
      (row_idx < col_idx - right_context)).astype(dtype) * large_negative_number
  return jnp.minimum(jnp.minimum(mask, padding_mask), rev_padding_mask)


def causal_mask(input_t: JTensor) -> JTensor:
  """Computes and returns causal mask.

  Args:
    input_t: A JTensor of shape [B, T, D].

  Returns:
    An attention_mask JTensor of shape [1, 1, T, T]. Attention mask has
    already been converted large negative values.
  """
  assert (input_t.dtype == jnp.float32 or
          input_t.dtype == jnp.bfloat16), input_t.dtype
  large_negative_number = py_utils.get_large_negative_number(input_t.dtype)
  t = input_t.shape[1]
  col_idx = jnp.tile(jnp.arange(t)[jnp.newaxis, :], [t, 1])
  row_idx = jnp.tile(jnp.arange(t)[:, jnp.newaxis], [1, t])
  mask = (row_idx < col_idx).astype(input_t.dtype) * large_negative_number
  return mask[jnp.newaxis, jnp.newaxis, :, :]


def segment_mask(segment_ids: JTensor,
                 source_segment_ids: Optional[JTensor] = None,
                 dtype: jnp.dtype = jnp.float32) -> JTensor:
  """Computes (non-causal) segment mask.

  Args:
    segment_ids: a JTensor of shape [B, T], the segment that each token belongs
      to.
    source_segment_ids: a JTensor of shape [B, S], the segment that each source
      token belongs to (optional).
    dtype: data type of the input.

  Returns:
    A JTensor of shape [B, 1, T, S].
  """
  # [B, T, 1]
  segment_ids_1 = jnp.expand_dims(segment_ids, axis=-1)
  # [B, 1, S]
  if source_segment_ids is not None:
    segment_ids_2 = jnp.expand_dims(source_segment_ids, axis=1)
  else:
    segment_ids_2 = jnp.expand_dims(segment_ids, axis=1)
  # [B, T, S].
  mask = jnp.not_equal(segment_ids_1, segment_ids_2).astype(dtype)
  mask = jnp.expand_dims(mask, 1)
  mask *= py_utils.get_large_negative_number(dtype)
  return mask


def causal_segment_mask(
    segment_ids: JTensor,
    dtype: jnp.dtype = jnp.float32,
    causal_attention_mask: Optional[JTensor] = None) -> JTensor:
  """Computes the masks which combines causal masking and segment masks.

  Args:
    segment_ids: a JTensor of shape [B, T], the segment that each token belongs
      to.
    dtype: data type of the input.
    causal_attention_mask: a JTensor of shape [B, T] where 1 indicates where a
      casual mask should be applied and 0 where it shouldn't. E.g. for an input
      -> target type of input. Tensor indices corresponding to the input tokens
      should be set to 0 and indices corresponding to target tokens should be
      set to 1.

  Returns:
    A JTensor of shape [B, 1, T, T].
  """
  # [B, 1, T, T]
  segment_mask_t = segment_mask(segment_ids, dtype=dtype)
  # [1, 1, T, T]
  b, t = segment_ids.shape
  causal_mask_t = causal_mask(jnp.zeros([b, t, 1], dtype=dtype))
  if causal_attention_mask is not None:
    causal_mask_t *= causal_attention_mask[:, jnp.newaxis, jnp.newaxis, :]
  return jnp.minimum(segment_mask_t, causal_mask_t)


def convert_paddings_to_mask(paddings: JTensor,
                             dtype: jnp.dtype = jnp.float32) -> JTensor:
  """Converts binary paddings to a logit mask ready to add to attention matrix.

  Args:
    paddings: binary JTensor of shape [B, T], with 1 denoting padding token.
    dtype: data type of the input.

  Returns:
    A JTensor of shape [B, 1, 1, T] ready to add to attention logits.
  """
  attention_mask = paddings[:, jnp.newaxis, jnp.newaxis, :]
  attention_mask *= py_utils.get_large_negative_number(dtype)
  return attention_mask


def shift_1d(inputs: JTensor, offset: int, axis: int):
  """Shift the input tensor by offset in the dimension axis.

    To shift right the offset is positive and the input is padded at the
    beginning, while to shift left the offset is negative and the input is
    padded at the end.

  Args:
    inputs: The input tensor to shift.
    offset: The number of positions to shift. If the offset is positive, pad at
      the beginning of the sequence, if the offset is negative, then pad at the
      end of the sequence.
    axis: The dimension in which to shift the input.

  Returns:
    The shifted input.
  """
  paddings = [((max(offset, 0), -min(offset, 0)) if i == axis else (0, 0))
              for i in range(len(inputs.shape))]
  input_length = jnp.shape(inputs)[axis]
  padded_inputs = jnp.pad(inputs, paddings)
  if offset > 0:
    output = jax.lax.slice_in_dim(
        padded_inputs, start_index=0, limit_index=input_length, axis=axis)
  else:
    output = jax.lax.slice_in_dim(
        padded_inputs,
        start_index=-offset,
        limit_index=input_length - offset,
        axis=axis)
  return output


def _convert_to_block(x, block_size: int, padding_val: float = 0.0) -> JTensor:
  """Turns a sequence to non overlapping blocks.

  Args:
    x: a tensor of [batch, time, ...].
    block_size: int. Number of time frames in a block.
    padding_val: float. value on the padded frames.

  Returns:
    A tensor of [batch, num_blocks, block_size, ...], with necessary paddings,
    where output[:, i, ...] are x[:, i*block_size:(i+1)*block_size, ...].
  """
  shape = list(x.shape)
  b, t = shape[0], shape[1]
  if block_size < 1:
    raise ValueError('block_size must be at least 1, got {}'.format(block_size))
  w = block_size
  # Pad t to be a multiply of w.
  num_blocks = (t + w - 1) // w
  pad_length = num_blocks * w - t

  if pad_length > 0:
    pad_shape = [
        (0, 0) if idx != 1 else (0, pad_length) for idx in range(len(x.shape))
    ]
    x = jnp.pad(x, pad_shape, constant_values=padding_val)
  reshaped = jnp.reshape(x, [b, num_blocks, w] + shape[2:])
  return reshaped


def _extract_block_context(x: JTensor,
                           block_size: int,
                           left_context: int,
                           right_context: int,
                           padding_val: float = 0.0) -> JTensor:
  """Extracts temporal context for every block.

  Args:
    x: a tensor of [batch, time, ...].
    block_size: int. Number of time frames in a block.
    left_context: int. Left context size.
    right_context: int. Right context size.
    padding_val: float. value on the padded frames.

  Returns:
    A tensor of [batch, num_blocks, context_size, ...], with necessary paddings,
    where context_size = block_size + (left_context - 1) + right_context,
    and output[:, i, ...] are x[:, start-left_context+1:end+right_context, ...],
    start = i * block_size, end = (i + 1) * block_size.
  """
  if block_size < 1:
    raise ValueError('block_size must be at least 1, got {}'.format(block_size))
  if left_context < 1 or left_context > block_size + 1:
    raise ValueError(
        'left_context must be at least 1 and at most block_size + 1 = {}, '
        'got {}'.format(block_size + 1, left_context))
  if right_context < 0 or right_context > block_size:
    raise ValueError(
        'right_context must be at least 0 and at most block_size = {}, '
        'got {}'.format(block_size, right_context))

  block = _convert_to_block(x, block_size, padding_val)
  concat_list = [block]

  if left_context > 1:
    if block_size == left_context - 1:
      left_block = jnp.roll(block, shift=1, axis=1)
    else:
      x_shift = jnp.roll(x, shift=left_context - 1, axis=1)
      x_shift_block = _convert_to_block(x_shift, block_size, padding_val)
      left_block = x_shift_block[:, :, :left_context - 1:, ...]
    concat_list = [left_block] + concat_list

  if right_context > 0:
    if block_size == right_context:
      right_block = jnp.roll(block, shift=-1, axis=1)
    else:
      x_shift = jnp.roll(x, shift=-right_context, axis=1)
      x_shift_block = _convert_to_block(x_shift, block_size, padding_val)
      right_block = x_shift_block[:, :, -right_context:, ...]
    concat_list += [right_block]

  return jnp.concatenate(concat_list, axis=2)


def _make_local_mask(seq_len: int, block_size: int, left_context: int,
                     right_context: int) -> JTensor:
  """Makes the mask tensor for a full sequence.

  The returned mask reflects the given context sizes, where position i
  attends to tokens in the range [i - (left_context-1), i + right_context].

  For example, given seq_len=4, block_size=2, left_context=3, right_context=0,
  the result mask is
  [[[0., 0., 1., 0.], 1st query in 1st block attends 1st key.
  [0., 0., 1., 1.]],  2nd query in 1st block attends 2nd and left keys
  [[1., 1., 1., 0.],  1st query in 2nd block attends 1st and left keys
  [0., 1., 1., 1.]]]  2st query in 2nd block attends 2nd and left keys

  Args:
    seq_len: int or scalar int tensor. Sequence length.
    block_size: int. Number of time frames in a block.
    left_context: int. Left context size.
    right_context: int. Right context size.

  Returns:
    A tensor of [num_blocks, block_size, context_size] taking values in
    {0, 1}, where context_size = block_size + (left_context - 1) + right_context
    Element b, i, j is 1 if in the b-th block, the i-th frame can access
    the j-th frame in the context.
  """
  assert seq_len > 0

  num_blocks = (seq_len + block_size - 1) // block_size
  context_size = block_size + (left_context - 1) + right_context

  # [num_blocks, block_size]: source positions in the original sequence.
  src_positions = jnp.reshape(
      jnp.arange(num_blocks * block_size), [num_blocks, block_size])
  # [num_blocks,]: source positions at the start of each block.
  block_start_positions = jnp.arange(0, num_blocks * block_size, block_size)
  # [context_size]:  positions relative to the block start.
  relative_context_positions = jnp.arange(context_size) - (left_context - 1)

  # [num_blocks, context_size]: target positions in the original sequence.
  tgt_positions = (
      block_start_positions[:, jnp.newaxis] +
      relative_context_positions[jnp.newaxis, :])
  # [num_blocks, block_size, context_size]: position differences between source-
  # target pairs.
  position_diff = src_positions[:, :,
                                jnp.newaxis] - tgt_positions[:, jnp.newaxis, :]
  # [num_blocks, block_size, context_size]: if attention is allowed between
  # source-target pairs.
  valid_atten = jnp.logical_and(-right_context <= position_diff,
                                position_diff < left_context)

  # [num_blocks, block_size]: if the source position is valid, not padded.
  valid_src = src_positions < seq_len
  # [num_blocks, context_size]: if the target position is valid, not padded.
  valid_tgt = jnp.logical_and(0 <= tgt_positions, tgt_positions < seq_len)

  valid_atten &= jnp.logical_and(valid_src[:, :, jnp.newaxis],
                                 valid_tgt[:, jnp.newaxis, :])

  return valid_atten


class PerDimScale(base_layer.BaseLayer):
  """A layer to scale individual dims of the input."""

  class HParams(BaseHParams):
    """Associated hyper-params for this layer class.

    Attributes:
      dim: Number of individual dims.
    """
    dim: int = 0

  def setup(self) -> None:
    p = self.hparams
    pc = WeightHParams(shape=[p.dim], init=WeightInit.Constant(0.0))
    self.create_variable('per_dim_scale', pc)

  def __call__(self, inputs: JTensor) -> JTensor:
    """Return per_dim_scale * inputs / jnp.sqrt(dim)).

    Args:
      inputs: A JTensor with shape [..., p.dim].

    Returns:
      outpus: A JTensor with shape [..., p.dim].
    """
    p = self.hparams
    inputs_shape = inputs.shape
    assert inputs_shape[-1] == p.dim

    # 1.0/jax.nn.softplus(0.0) = 1.442695041. Hard code this number so that we
    # can avoid unnecessary XLA op fusion mess on TPU.
    r_softplus_0 = 1.442695041
    scale = jnp.array(r_softplus_0 / np.sqrt(p.dim), dtype=inputs.dtype)
    scale *= jax.nn.softplus(self.theta.per_dim_scale)
    return inputs * scale


class RelativeBias(base_layer.BaseLayer):
  """A layer for Relative Attention Bias.

  Paper: https://aclanthology.org/N18-2074.pdf.
  Note that attention bias ensures that current position (~row) is less that
  memory position(~column).

  In addition to masking bias we use per-head per-relative position bucket
  relative_bias_weights array of shape
  [num heads, num relative position buckets].

  We compute relative position bucket for every position pair, relative_bucket
  tensor of shape [batch, length, length] and do
  jax.lax.gather(operand=relative_bias_weights, start_indices=relative_bucket,
    dimension_numbers=jax.lax.GatherDimensionNumbers(
        offset_dims=tuple(1)),
  to compute per position-pair bias.
  """

  class HParams(BaseHParams):
    """Associated hyper-params for this layer class.

    Attributes:
      num_heads: Number of attention heads.
      use_length_as_position: If true, use length as position to save some
        memory. Relative bias is based on relative position indexes thus we can
        ignore segments.
      relative_attention_num_buckets: Number of buckers for relative attention.
      relative_attention_max_distance: Maximum relative distance (outer bucket
        boundary).
      bidirectional: If true, use half of the buckets for forward-looking
        attention.
      use_xavier_init: If true, use xavier init for the buckets.
    """
    num_heads: int = 1
    use_length_as_position: bool = False
    relative_attention_num_buckets: int = 32
    relative_attention_max_distance: int = 128
    bidirectional: bool = False
    use_xavier_init: bool = False

  def setup(self) -> None:
    p = self.hparams
    if p.use_xavier_init:
      init = WeightInit.Xavier()
    else:
      rb_stddev = (p.num_heads * p.relative_attention_num_buckets)**-0.5
      init = WeightInit.Gaussian(rb_stddev)
    pc = WeightHParams(
        shape=[p.num_heads, p.relative_attention_num_buckets], init=init)
    self.create_variable('wrb', pc)

  def _relative_position_bucket(self, relative_position: JTensor) -> JTensor:
    """Translate relative position to a bucket number for relative attention.

    Args:
      relative_position: An int32 JTensor.

    Returns:
      A JTensor with the same shape as relative_position, containing int32
      values in the range [0, num_buckets)
    """
    p = self.hparams

    num_buckets = p.relative_attention_num_buckets
    max_distance = jnp.array(p.relative_attention_max_distance).astype(p.dtype)
    ret = 0
    n = -relative_position
    if p.bidirectional:
      num_buckets //= 2
      ret += ((n < 0) * num_buckets).astype(jnp.int32)
      n = jnp.abs(n)
    else:
      n = jnp.maximum(n, 0)
    # now n is in the range [0, inf)
    max_exact = num_buckets // 2
    is_small = jnp.less(n, max_exact)
    val_if_large = max_exact + (jnp.log(n.astype(p.dtype) / max_exact) /
                                jnp.log(max_distance / max_exact) *
                                (num_buckets - max_exact)).astype(jnp.int32)
    val_if_large = jnp.minimum(val_if_large, num_buckets - 1)
    ret += jnp.where(is_small, n, val_if_large)
    return ret

  def __call__(self,
               query_segment_pos: JTensor,
               key_segment_pos: Optional[JTensor] = None) -> JTensor:
    """Return relative bias for attention.

    We use the following capital letters to denote certain JTensor parameters.

      B = batch size
      S = length of the key/value (source)
      T = length of the query (target)
      N = number of attention heads

    When training query_segment_pos = key_segment_pos, of shape [batch, time].
    When decoding query_segment_pos is [batch, beam_size]
    but key_segment_pos is [batch, memory_size] (because of k_pos StateLayer).

    Args:
      query_segment_pos: A JTensor with shape [B, T].
      key_segment_pos: A JTensor with shape [B, S].

    Returns:
      relative_bias: A JTensor with shape [B, N, T, S], where batch == 1 if
        p.use_length_as_position is True.
    """
    p = self.hparams
    asserts.not_none(query_segment_pos)
    if key_segment_pos is None:
      key_segment_pos = query_segment_pos

    # Relative position is defined in such a way that when query is in the
    # future relative to the key, the value of relative position is negative.
    if p.use_length_as_position:
      klen = key_segment_pos.shape[1]
      qlen = query_segment_pos.shape[1]
      key_pos = np.arange(klen, dtype=jnp.int32)[None, None, :]
      query_pos = np.arange(qlen, dtype=jnp.int32)[None, :, None]
      relative_position = key_pos - query_pos
    else:
      relative_position = (
          jnp.expand_dims(key_segment_pos, -2) -
          jnp.expand_dims(query_segment_pos, -1))
    relative_bucket = self._relative_position_bucket(relative_position)

    relative_bucket_one_hot = jax.nn.one_hot(
        relative_bucket, p.relative_attention_num_buckets, dtype=p.dtype)
    # relative_bucket_one_hot:
    # BTSX - [batch, length, memory_length, num_buckets]
    #
    # relative bias weights theta.wrb:
    # NX - [num_heads, num_buckets]
    #
    # relative_bias:
    # [batch, heads, length, memory_length]
    relative_bias = jnp.einsum('NX,BTSX->BNTS', self.theta.wrb,
                               relative_bucket_one_hot)

    # Eventually we add bias to BNTS [batch, heads, length, memory_length]
    # logits tensor, so we make 'heads' dim next to batch, where batch == 1 if
    # p.use_length_as_position is True.
    return relative_bias

  def extend_step(self,
                  seq_length: int,
                  time_step: Optional[Union[int, JTensor]] = None) -> JTensor:
    """Generates a JTensor for a step in greedy search.

    B = batch size
    P = prefix length
    S = sequence length
    N = number of attention heads

    Args:
      seq_length: An integer equal to S.
      time_step: The time step which is being decoded.

    Returns:
      relative_bias: A JTensor with shape [1, N, 1, S].
    """
    query_segment_pos = jnp.zeros([1], jnp.int32) + time_step
    key_segment_pos = jnp.arange(seq_length, dtype=jnp.int32)
    relative_bias = self(
        query_segment_pos=query_segment_pos[jnp.newaxis, :],
        key_segment_pos=key_segment_pos[jnp.newaxis, :])
    return relative_bias


class AttentionProjection(base_layer.BaseLayer):
  """Layer that computes multi heads projection.

    This layer is expected to be used within DotProductAttention below.
  """

  class HParams(BaseHParams):
    """Associated hyper-params for this layer class.

    Attributes:
      input_dim: Input dimension.
      num_heads: Number of attention heads.
      dim_per_head: Size of each head.
      is_output_projection: Whether it is out projection or not. If False, we
        use "...D,DNH->...NH" for query,key,value projection. Otherwise we use
        "...NH,DNH->...D" for output projection.
      use_bias: Whether to add bias in projection or not.
      attention_combine_dims: The heads and key/value dimensions are combined in
        the variables and the computation.
      use_nhd_shape: Whether to use NHD shape for the variable, useful for dot
        attention output layer.
    """
    input_dim: int = 0
    num_heads: int = 0
    dim_per_head: int = 0
    is_output_projection: bool = False
    use_bias: bool = True
    attention_combine_dims: bool = False
    use_nhd_shape: bool = False

  def setup(self) -> None:
    p = self.hparams
    wp = p.weight_split_dims_mapping
    if p.mesh_shape is not None:
      assert wp.wt is not None, ('Must provide sharding annotations for the '
                                 'weights if mesh shape is provided')
    if p.attention_combine_dims:
      assert not p.use_bias
      hd_shape = [p.num_heads * p.dim_per_head]
    else:
      hd_shape = [p.num_heads, p.dim_per_head]

    if (p.attention_combine_dims and isinstance(wp.wt, list) and
        len(wp.wt) == 3):
      wt = [axis for axis in wp.wt if axis is not None]
      assert len(wt) == 2
    else:
      wt = wp.wt
    pc_shape = [p.input_dim] + hd_shape
    if p.is_output_projection and p.use_nhd_shape:
      pc_shape = hd_shape + [p.input_dim]
    pc = WeightHParams(
        shape=pc_shape, mesh_shape=p.mesh_shape, tensor_split_dims_mapping=wt)
    self.create_variable('w', pc)
    if p.use_bias:
      if p.is_output_projection:
        if p.mesh_shape is not None:
          bias_split_dims_mapping = [wp.wt[0]]
        else:
          bias_split_dims_mapping = None
        pc_bias = WeightHParams(
            shape=[p.input_dim],
            init=WeightInit.Constant(0.0),
            mesh_shape=p.mesh_shape,
            tensor_split_dims_mapping=bias_split_dims_mapping)
      else:
        if p.mesh_shape is not None:
          bias_split_dims_mapping = [wp.wt[1], wp.wt[2]]
        else:
          bias_split_dims_mapping = None
        pc_bias = WeightHParams(
            shape=[p.num_heads, p.dim_per_head],
            init=WeightInit.Constant(0.0),
            mesh_shape=p.mesh_shape,
            tensor_split_dims_mapping=bias_split_dims_mapping)
      self.create_variable('b', pc_bias)

  def __call__(self, inputs: JTensor) -> JTensor:
    """Computes the multi headed projection for inputs.

    Args:
      inputs: A JTensor of shape [..., num_heads, dim_per_head] if
        p.is_output_projection is True or [..., p.input_dim] otherwise..

    Returns:
      The projected JTensor with shape [..., p.input_dim] if
      p.is_output_projection is True or [..., num_heads, dim_per_head]
      otherwise.
    """
    p = self.hparams
    theta = self.theta

    # Because tf.einsum is not fully optimized unless all the dimensions are
    # fully specified, we have to avoid using '...' for batch dimensions in the
    # equation in tf.einsum for optimized performance. This is only feasible
    # when the rank of the tensor is known.
    # Sort the available symbols to avoid nondeterminism.
    eqn_sym = ''.join(sorted(set(string.ascii_uppercase) - set('DHN')))
    shape = inputs.shape
    rank = len(shape)

    inputs = self._cast_to_fprop_dtype(inputs)
    if p.attention_combine_dims:
      pc_shape = [p.input_dim, p.num_heads, p.dim_per_head]
      if p.is_output_projection and p.use_nhd_shape:
        pc_shape = [p.num_heads, p.dim_per_head, p.input_dim]
      w = jnp.reshape(theta.w, pc_shape)
    else:
      w = theta.w

    if p.is_output_projection:
      assert shape[-2:] == (p.num_heads, p.dim_per_head)
      batch_eqn = eqn_sym[:(rank - 2)]
      if p.use_nhd_shape:
        eqn = f'{batch_eqn}NH,NHD->{batch_eqn}D'
      else:
        eqn = f'{batch_eqn}NH,DNH->{batch_eqn}D'
    else:
      assert shape[-1] == p.input_dim, (
          f'Expecting shape[-1] == p.input_dim, {shape[-1]} != {p.input_dim}')
      batch_eqn = eqn_sym[:(rank - 1)] if rank else '...'
      eqn = f'{batch_eqn}D,DNH->{batch_eqn}NH'
    ret = jnp.einsum(eqn, inputs, w)
    if p.use_bias:
      ret += theta.b
    return ret


class CombinedQKVProjectionLayer(base_layer.BaseLayer):
  """Layer that computes QKV projection with a combined weight.

  It may lead to faster collectives and step-time on TPU.

  This layer is expected to be used within DotProductAttention below.
  """

  class HParams(BaseHParams):
    """Associated hyper-params for this layer class.

    Attributes:
      input_dim: Input dimension.
      num_heads: Number of heads.
      dim_per_head: Size of each head.
      use_bias: Whether to add bias in the projection layer.
      attention_combine_dims: If set, the heads and key/value dimensions are
        combined in the variables and the computation.
    """
    input_dim: int = 0
    num_heads: int = 0
    dim_per_head: int = 0
    use_bias: bool = True
    attention_combine_dims: bool = False

  def setup(self) -> None:
    p = self.hparams
    wp = p.weight_split_dims_mapping
    if p.mesh_shape is not None:
      assert wp.wt is not None, ('Must provide sharding annotations for the '
                                 'weights if mesh shape is provided')
      if (p.attention_combine_dims and isinstance(wp.wt, list) and
          len(wp.wt) == 3):
        wt = [axis for axis in wp.wt if axis is not None]
        assert len(wt) == 2, ('wp.wt only specifies the sharding for '
                              'the last two dims of the weight tensor.')
      else:
        wt = wp.wt
        # Replicate the concat axis.
        assert len(wt) == 3, ('wp.wt only specifies the sharding for '
                              'the last three dims of the weight tensor.')
      weight_split_dims_mapping = [None] + list(wt)
      if p.attention_combine_dims:
        bias_split_dims_mapping = [None, wt[1]]
      else:
        bias_split_dims_mapping = [None, wt[1], wt[2]]
    else:
      weight_split_dims_mapping = None
      bias_split_dims_mapping = None

    if p.attention_combine_dims:
      hd_shape = [p.num_heads * p.dim_per_head]
    else:
      hd_shape = [p.num_heads, p.dim_per_head]

    pc_shape = [3, p.input_dim] + hd_shape
    # Combined weight for q, k, v projections.
    pc = WeightHParams(
        shape=pc_shape,
        init=p.params_init,
        dtype=p.dtype,
        mesh_shape=p.mesh_shape,
        tensor_split_dims_mapping=weight_split_dims_mapping)
    self.create_variable('w', pc)
    if p.use_bias:
      # Combined bias weight for q, k, v projections.
      pc_bias = WeightHParams(
          shape=[3] + hd_shape,
          init=WeightInit.Constant(0.0),
          mesh_shape=p.mesh_shape,
          tensor_split_dims_mapping=bias_split_dims_mapping)
      self.create_variable('b', pc_bias)

  # TODO(zhangqiaorjc): Take query, key, value as inputs to support all
  # attentions.
  def __call__(self, inputs: JTensor) -> Tuple[JTensor, JTensor, JTensor]:
    """Computes the QKV projection for inputs.

    Args:
      inputs: A JTensor of shape [..., p.input_dim].

    Returns:
      The three projected JTensor with shape [..., num_heads, dim_per_head]
      in q_proj, k_proj and v_proj order.
    """
    p = self.hparams
    theta = self.theta

    # Because tf.einsum is not fully optimized unless all the dimensions are
    # fully specified, we have to avoid using '...' for batch dimensions in the
    # equation in tf.einsum for optimized performance. This is only feasible
    # when the rank of the tensor is known.
    # Sort the available symbols to avoid nondeterminism.
    eqn_sym = ''.join(sorted(set(string.ascii_uppercase) - set('KDHN')))
    shape = inputs.shape
    rank = len(shape)
    assert rank > 0

    assert shape[-1] == p.input_dim
    batch_dims_rank = rank - 1
    batch_eqn = eqn_sym[:batch_dims_rank] if rank else '...'
    if p.attention_combine_dims:
      pc_shape = [3, p.input_dim, p.num_heads, p.dim_per_head]
      w = jnp.reshape(theta.w, pc_shape)
      if p.use_bias:
        b_shape = [3, p.num_heads, p.dim_per_head]
        b = jnp.reshape(theta.b, b_shape)
    else:
      w = theta.w
      if p.use_bias:
        b = theta.b

    # K indexes qkv.
    eqn = f'{batch_eqn}D,KDNH->K{batch_eqn}NH'
    ret = jnp.einsum(eqn, inputs, w)
    ret = checkpoint_name(ret, 'combined_qkv_proj')
    if p.use_bias:
      # Add newaxis to bias weight for each batch dim since ret is K...NH
      # and theta.b is KNH. Need to reshape theta.b to K...NH
      ret += jnp.expand_dims(b, list(range(1, batch_dims_rank + 1)))
    # Split into three projections.
    query_proj, key_proj, value_proj = ret
    query_proj = checkpoint_name(query_proj, 'query_proj')
    key_proj = checkpoint_name(key_proj, 'key_proj')
    value_proj = checkpoint_name(value_proj, 'value_proj')
    return query_proj, key_proj, value_proj


class DotProductAttention(base_layer.BaseLayer):
  """Dot-product attention with multiple attention heads.

  This implementation heavily uses einsum to be efficient on TPUs.  We use the
  following capital letters to denote certain JTensor parameters.

    B = batch size
    S = length of the key/value (source)
    T = length of the query (target)
    D = model dimension
    N = number of attention heads
    H = dimensions of each attention head.

  The algorithm is sketched as follows. Each intermediate JTensor or weight
  JTensor is annotated with its shape. E.g., Wq, the weight JTensor for query's
  projection, its shape is [D, N, H].

  Trainable weights:
    Wq, Wk, Wv: [D{q,k,v}, N, H]
    Wout: [Dq, N, H]

  Note it also allows k, v and q to have different input dimension by setting
  input_dim as a dict: {'key': key_dim, 'value': value_dim, 'query': query_dim}.

  Input q:[B, T, Dq]; k:[B, S, Dk]; v:[B, S, Dv]
  q_proj: [B, T, N, H] = einsum('BTD,DNH->BTNH', x, Wq)
  k_proj: [B, S, N, H] = einsum('BSD,DNH->BSNH', x, Wk)
  v_proj: [B, S, N, H] = einsum('BSD,DNH->BSNH', x, Wv)
  logits: [B, N, T, S] = einsum('BTNH,BSNH->BNTS', q_proj, k_proj) / sqrt(H)
  probs:  [B, N, T, S] = softmax(logits, axis=-1)
  context:[B, T, N, H] = einsum('BNTS,BSNH->BTNH', probs, v_proj)
  output: [B, T, Dq]   = einsum('BTNH,DNH>BTD', context, Wout)
  """

  class HParams(BaseHParams):
    """Associated hyper-params for this layer class.

    Attributes:
      input_dim: An integer or a dict of integer values as number of input
        nodes. If input_dim is a dict, keys must be key, value and query.
      hidden_dim: Number of hidden nodes.
      num_heads: Number of attention heads.
      dim_per_head: Dimension of each attention head. If None then dim_per_head
        == hidden_dim // num_heads.
      dropout_tpl: Parameterization for the dropout layer.
      atten_dropout_prob: Probability at which we apply dropout to the attention
        weights.
      proj_tpl: Parameterization for the projection layer.
      dconv_qkv: If True then apply a depth-wise convolution of
        `dconv_kernel_size`x1 after the key, query and value projection as in
        Primer https://arxiv.org/abs/2109.08668. Note that this is currently
          only supported for self-attention.
      dconv_kernel_size: Size of the kernel window over the sequence dimension
        in the depth-wise convolution.
      internal_gshard_gaussian_init: Projection weight init follows Gaussian
        distribution.
      combine_qkv: Whether to combine qkv tensor for optimizing qkv input
        gradient computation with SPMD. Only supports self-attention.
      combined_qkv_proj_tpl: Parameterization for combined QKV projection layer.
      use_bias: Whether to use bias for projection layers.
      output_proj_use_nhd_shape: Whether to use NHD variable shape in output
        projection layer.
      internal_enable_query_scale: Internal. Enable scaling of query vector.
      internal_enable_per_dim_scale: Internal. Setting to False disables
        rescaling of attention logits with 1/sqrt(dim) factor. Some Transformer
        variants (GShard, T5) use internal_enable_per_dim_scale=False and adjust
        initialization of the linear transformations(einsums), in conjunction
        with Adafactor optimizer.
      atten_logit_cap: Cap the absolute values of logits by tanh. Enabled when a
        positive value is specified. May not be supported by a subclass.
      use_rotary_position_emb: Whether to add rotary position embedding to the
        queries and keys before computing self attention scores. This was
        proposed in https://arxiv.org/abs/2104.09864.
      relative_bias_tpl: Optional parameterization of relative bias.
      attention_extra_logit: Extra logit for attention softmax.
      ngrammer_tpl: Params for the Ngrammer layer. This param must correspond to
        the VQNgrammer layer. If this is None, then there is no NGrammer layer
        present in this layer.
      decode_cache: if the attention layer needs decode cache.
    """
    input_dim: Union[int, Dict[str, int]] = 0
    hidden_dim: int = 0
    num_heads: int = 1
    dim_per_head: Optional[int] = None
    dropout_tpl: BaseHParams = sub_config_field(stochastics.Dropout.HParams)
    atten_dropout_prob: float = 0.0
    proj_tpl: BaseHParams = sub_config_field(AttentionProjection.HParams)
    dconv_qkv: bool = False
    dconv_kernel_size: int = 3
    internal_gshard_gaussian_init: bool = False
    combine_qkv: bool = False
    combined_qkv_proj_tpl: BaseHParams = sub_config_field(
        CombinedQKVProjectionLayer.HParams)
    use_bias: bool = True
    output_proj_use_nhd_shape: bool = False
    internal_enable_query_scale: bool = True
    internal_enable_per_dim_scale: bool = True
    atten_logit_cap: float = 0.0
    use_rotary_position_emb: bool = False
    relative_bias_tpl: Optional[BaseHParams] = None
    attention_extra_logit: Optional[float] = None
    ngrammer_tpl: Optional[BaseHParams] = None
    decode_cache: bool = True

  # SPMD partition related params.
  #
  # d - model_dim
  # n - num_heads
  # h - attention_dim_per_heads
  # b - batch_size
  # l - seq_len

  class WeightShardingHParams(BaseWtShardingHParams):
    """Represents how layer's learned parameters are partitioned across a mesh.

    Attributes:
      proj: How the projection weights should be sharded. All projection matrix
        share the same sharding.
      dconv: How the dconv weights should be sharded. All dconv weights share
        the same sharding.
    """
    proj: SplitDimsMapping = None
    dconv: SplitDimsMapping = None

  class ActivationShardingHParams(BaseActShardingHParams):
    """Represents how intermediate values should be partitioned across a mesh.

    Attributes:
      blnh: Mesh split for query, key, value, and encoded tensors with the shape
        of [batch_size, seq_len, num_heads, dim_per_head].
      bld: Mesh split for output after post projection with the shape of
        [batch_size, seq_len, model_dim].
    """
    blnh: SplitDimsMapping = None
    bld: SplitDimsMapping = None

  def setup(self) -> None:
    p = self.hparams
    wp = p.weight_split_dims_mapping
    assert p.input_dim, 'input_dim is {}'.format(p.input_dim)
    assert p.hidden_dim, 'hidden_dim is {}'.format(p.hidden_dim)

    dim_per_head = p.dim_per_head
    if dim_per_head is None:
      dim_per_head = p.hidden_dim // p.num_heads
      p.dim_per_head = dim_per_head
      assert dim_per_head * p.num_heads == p.hidden_dim, (
          f'{dim_per_head} * {p.num_heads} != {p.hidden_dim}')

    if p.mesh_shape is not None:
      assert p.weight_split_dims_mapping is not None
      assert p.activation_split_dims_mapping is not None

    def project_input(input_dim, gaussian_std=None):
      proj_p = p.proj_tpl.clone().set(
          input_dim=input_dim,
          num_heads=p.num_heads,
          dim_per_head=dim_per_head,
          use_bias=p.use_bias)
      if gaussian_std:
        proj_p.params_init = WeightInit.Gaussian(gaussian_std)
      proj_p.weight_split_dims_mapping.wt = wp.proj
      return proj_p

    def combined_qkv_project_input(input_dim):
      proj_p = p.combined_qkv_proj_tpl.clone().set(
          input_dim=input_dim,
          num_heads=p.num_heads,
          dim_per_head=dim_per_head,
          use_bias=p.use_bias)
      proj_p.weight_split_dims_mapping.wt = wp.proj
      return proj_p

    if isinstance(p.input_dim, dict):
      key_input_dim = p.input_dim['key']
      value_input_dim = p.input_dim['value']
      query_input_dim = p.input_dim['query']
      assert key_input_dim, f'key_input_dim is {key_input_dim}'
      assert query_input_dim, f'query_input_dim is {query_input_dim}'
    else:
      key_input_dim = p.input_dim
      value_input_dim = p.input_dim
      query_input_dim = p.input_dim

    if p.internal_gshard_gaussian_init:
      query_std = (query_input_dim * dim_per_head)**-0.5
      key_std = (key_input_dim)**-0.5
      value_std = (value_input_dim)**-0.5
      post_std = (p.num_heads * dim_per_head)**-0.5
    else:
      query_std = None
      key_std = None
      value_std = None
      post_std = None

    if p.combine_qkv:
      assert key_input_dim == value_input_dim
      assert key_input_dim == query_input_dim
      self.create_child('combined_qkv',
                        combined_qkv_project_input(query_input_dim))
    else:
      self.create_child('key', project_input(key_input_dim, key_std))
      self.create_child('query', project_input(query_input_dim, query_std))
      self.create_child('value', project_input(value_input_dim, value_std))

    if p.use_rotary_position_emb:
      pos_emb_p = embedding_softmax.RotaryPositionalEmbedding.HParams()
      pos_emb_p.embedding_dims = dim_per_head
      self.create_child('rotary_position_emb', pos_emb_p)

    if p.relative_bias_tpl is not None:
      relative_bias_p = p.relative_bias_tpl
      relative_bias_p.num_heads = p.num_heads
      self.create_child('relative_bias', relative_bias_p)

    if p.dconv_qkv:
      causal_dconv_p = CausalDepthwiseConv1D.HParams(
          kernel_size=p.dconv_kernel_size,
          hidden_dims=[p.num_heads, dim_per_head],
      )
      causal_dconv_p.weight_split_dims_mapping.wt = wp.dconv
      self.create_child('dconv_q', causal_dconv_p)
      self.create_child('dconv_k', causal_dconv_p)
      self.create_child('dconv_v', causal_dconv_p)

    # Initialize NGrammer layer if present
    if p.ngrammer_tpl is not None:
      self.create_child('ngrammer', p.ngrammer_tpl)

    if p.internal_enable_query_scale and p.internal_enable_per_dim_scale:
      self.create_child('per_dim_scale', PerDimScale.HParams(dim=dim_per_head))
    self.create_child('atten_dropout',
                      p.dropout_tpl.set(keep_prob=1.0 - p.atten_dropout_prob))
    # Setting is_output_projection=True to set the projection direction
    # from hidden dim to input dim. Output projection follows query_input_dim.
    post_proj_p = p.proj_tpl.clone().set(
        input_dim=query_input_dim,
        num_heads=p.num_heads,
        dim_per_head=dim_per_head,
        is_output_projection=True,
        use_bias=p.use_bias,
        use_nhd_shape=p.output_proj_use_nhd_shape)
    if post_std is not None:
      post_proj_p.params_init = WeightInit.Gaussian(post_std)
    if p.output_proj_use_nhd_shape and isinstance(wp.proj, list) and len(
        wp.proj) == 3:
      permutation = [1, 2, 0]
      post_proj_p.weight_split_dims_mapping.wt = [
          wp.proj[i] for i in permutation
      ]
    else:
      post_proj_p.weight_split_dims_mapping.wt = wp.proj

    self.create_child('post', post_proj_p)

  def _shard_bnh(self, x: JTensor) -> JTensor:
    """Shards tensors of shape [b, n, h].

    Single step decoder output are of shape [b, n, h].

    Args:
      x: A tensor of shape [b, n, h]

    Returns:
      x with proper sharding annotations.
    """
    p = self.hparams
    ap = p.activation_split_dims_mapping
    if p.mesh_axis_names is None:
      return x
    if ap.blnh is None:
      return x
    assert len(ap.blnh) == 4
    bnh = [ap.blnh[0], ap.blnh[2], ap.blnh[3]]
    return base_layer.maybe_shard(x, bnh, p.mesh_axis_names)

  def _shard_blnh(self, x: JTensor) -> JTensor:
    """Adds sharding annotations to tensors of shape [b, l, n, h]."""
    p = self.hparams
    ap = p.activation_split_dims_mapping
    return base_layer.maybe_shard(x, ap.blnh, p.mesh_axis_names)

  def _shard_bld(self, x: JTensor) -> JTensor:
    """Adds sharding annotations to tensors of shape [b, l, d]."""
    p = self.hparams
    ap = p.activation_split_dims_mapping
    return base_layer.maybe_shard(x, ap.bld, p.mesh_axis_names)

  def _shard_bd(self, x: JTensor) -> JTensor:
    """Adds sharding annotations to tensors of shape [b, d]."""
    p = self.hparams
    ap = p.activation_split_dims_mapping
    if p.mesh_axis_names is None:
      return x
    if ap.bld is None:
      return x
    assert len(ap.bld) == 3
    bd = [ap.bld[0], ap.bld[2]]
    return base_layer.maybe_shard(x, bd, p.mesh_axis_names)

  def _scale_query(self, query: JTensor) -> JTensor:
    """When enabled, scale the query vector."""
    p = self.hparams
    if p.internal_enable_query_scale:
      if p.internal_enable_per_dim_scale:
        query = self.per_dim_scale(query)
      else:
        query *= (p.hidden_dim // p.num_heads)**-0.5
    return query

  def _cap_logits(self, logits: JTensor) -> JTensor:
    """When enabled, caps the logits by p.atten_logit_cap with tanh."""
    p = self.hparams
    if not p.atten_logit_cap or p.atten_logit_cap <= 0.:
      return logits
    cap = jnp.array(p.atten_logit_cap, dtype=logits.dtype)
    # Note that since this caps the negative side as well, caller
    # must defer the pad-with-very-negative-logits logic to after
    # this function returns.
    logits = cap * jnp.tanh(logits / cap)
    return logits

  def _log_softmax_with_extra_logit(self, logits: JTensor) -> JTensor:
    """Compute log softmax with extra logit.

    self.hparams.attention_extra_logit is a user defined float value that
    helps to stablize logit values so that they don't drift too much from it.

    Args:
      logits: input logit tensor

    Returns:
      Log softmax with extra logit value.
    """
    # Applies stop_gradient to max_logit instead of logits.
    max_logit = jnp.max(jax.lax.stop_gradient(logits), axis=-1, keepdims=True)
    extra_logit = self.hparams.attention_extra_logit
    if extra_logit is not None:
      extra_logit = jnp.asarray(extra_logit, dtype=max_logit.dtype)
      max_logit = jnp.maximum(max_logit, extra_logit)
    exp_x = jnp.exp(logits - max_logit)
    sum_exp_x = jnp.sum(exp_x, axis=-1, keepdims=True)
    if extra_logit is not None:
      sum_exp_x += jnp.exp(extra_logit - max_logit)
    return logits - jnp.log(sum_exp_x) - max_logit

  def _atten_logits(self, query: JTensor, key: JTensor) -> JTensor:
    """Compute logits from query and key."""
    logits = jnp.einsum('BTNH,BSNH->BNTS', query, key)
    return logits

  def _dot_atten(
      self,
      query: JTensor,
      key: JTensor,
      value: JTensor,
      atten_mask: JTensor,
      relative_bias: Optional[JTensor] = None) -> Tuple[JTensor, JTensor]:
    """Main attention function.

    Args:
      query: JTensor of shape [B, T, N, H].
      key: JTensor of shape [B, S, N, H].
      value: JTensor of shape [B, S, N, H].
      atten_mask: JTensor of shape [1/B, 1, 1/T, S] which is a mask that is
        applied to prevent attention between unwanted pairs. This has already
        been converted into large negative logits. Note that the first and third
        dimension allow size 1 if the mask is shared by every item in the batch
        or every token in the target sequence.
      relative_bias: Relative bias of shape [B, N, T, S].

    Returns:
      encoded: JTensor of shape [B, T, N, H].
      atten_probs: JTensor of shape [B, N, T, S].
    """
    # Add key sharding annotations.
    p = self.hparams
    query = self._shard_blnh(query)
    key = self._shard_blnh(key)
    value = self._shard_blnh(value)

    b, s, n, h = key.shape
    base_layer.assert_has_shape(value, [b, s, n, h])
    base_layer.assert_has_shape(query, [b, -1, n, h])
    t = query.shape[1]
    # If only padding bias is supplied, then atten_mask can be [B, 1, 1, S]
    # since each target token is prohibited from attending to the same set of
    # source tokens. In this case tiling is inefficient and unnecessary.
    # If there is no padding mask, and only causal mask then the shape can be
    # [1, 1, T, S]
    base_layer.assert_has_shape(atten_mask, [-1, 1, -1, s])
    assert atten_mask.shape[2] in [1, t]
    assert atten_mask.shape[0] in [1, b]
    query = self._scale_query(query)
    logits = self._atten_logits(query, key)
    if relative_bias is not None:
      # The relative_bias has shape [1, n, t, s] or [b, n, t, s].
      base_layer.assert_has_shape(relative_bias, [-1, n, t, s])
      logits += relative_bias
    logits = checkpoint_name(logits, 'logits')
    logits = self._cap_logits(logits)
    # Attention softmax is always carried out in fp32.
    logits = logits.astype(jnp.float32)
    # Apply attention masking
    padded_logits = logits + atten_mask.astype(jnp.float32)
    if p.attention_extra_logit is None:
      probs = jax.nn.softmax(padded_logits, axis=-1).astype(key.dtype)
    else:
      probs = jnp.exp(self._log_softmax_with_extra_logit(padded_logits)).astype(
          key.dtype)
    # Apply attention dropout.
    probs = self.atten_dropout(probs)
    # Compute the attention context.
    encoded = jnp.einsum('BNTS,BSNH->BTNH', probs, value)
    encoded = checkpoint_name(encoded, 'context')
    encoded = self._shard_blnh(encoded)
    return encoded, probs

  def decoding_state_sequence_length(self):
    """Returns the length of full decoding sequences."""
    return self.get_decode_state('key_state').shape[1]

  def _dot_atten_one_step(self,
                          query: JTensor,
                          key_state_name: str,
                          value_state_name: str,
                          atten_mask: JTensor,
                          relative_bias: Optional[JTensor] = None) -> JTensor:
    """Dot attention function for queries with 1 time step.

    Args:
      query: JTensor of shape [B, N, H].
      key_state_name: Name of the decoding key state variable.
      value_state_name: Name of the decoding value state variable.
      atten_mask: JTensor of shape [1/B, 1, S] which is a mask that is applied
        to prevent attention between unwanted pairs. This has already been
        converted into large negative logits. The first dimension is allowed to
        be of size 1, if the mask is shared by all items in the batch (e.g.,
        only a causal mask).
      relative_bias: Relative bias of shape [1/B, N, 1, S].

    Returns:
      encoded: JTensor of shape [B, N, H].
      probs: JTensor of shape [B, N, S].
    """

    p = self.hparams
    key = self._shard_blnh(self.get_decode_state(key_state_name))
    value = self._shard_blnh(self.get_decode_state(value_state_name))
    # query is 3d.
    query = self._shard_bnh(query)

    b, s, n, h = key.shape
    base_layer.assert_has_shape(value, [b, s, n, h])
    base_layer.assert_has_shape(query, [b, n, h])
    base_layer.assert_has_shape(atten_mask, [-1, 1, s])
    assert atten_mask.shape[0] in [1, b]
    query = self._scale_query(query)
    logits = jnp.einsum('BNH,BSNH->BNS', query, key)
    if relative_bias is not None:
      base_layer.assert_has_shape(relative_bias, [-1, n, 1, s])
      assert relative_bias.shape[0] in [1, b]
      relative_bias = jnp.squeeze(relative_bias, axis=2)
      logits += relative_bias
    logits = self._cap_logits(logits)
    # Attention softmax is always carried out in fp32.
    logits = logits.astype(jnp.float32)
    # Apply attention masking
    padded_logits = logits + atten_mask.astype(jnp.float32)
    # Of shape [b, n, s]
    if p.attention_extra_logit is None:
      probs = jax.nn.softmax(padded_logits, axis=-1).astype(key.dtype)
    else:
      probs = jnp.exp(self._log_softmax_with_extra_logit(padded_logits)).astype(
          key.dtype)
    # Compute the attention context.
    encoded = jnp.einsum('BNS,BSNH->BNH', probs, value)
    encoded = self._shard_bnh(encoded)
    return encoded, probs

  def __call__(
      self,
      query_vec: JTensor,
      key_vec: JTensor,
      value_vec: JTensor,
      atten_mask: JTensor,
      query_segment_pos: Optional[JTensor] = None,
      key_segment_pos: Optional[JTensor] = None) -> Tuple[JTensor, JTensor]:
    """Computes the value vector given the current query output.

    Args:
      query_vec: JTensor of shape [B, T, D].
      key_vec: JTensor of shape [B, S, D].
      value_vec: JTensor of shape [B, S, D].
      atten_mask: JTensor of shape [1/B, 1, 1/T, S] which is a mask that is
        applied to prevent attention between unwanted pairs. This has already
        been converted into large negative logits. Note that the first and third
        dimension allow size 1 if the mask is shared by every item in the batch
        or every token in the target sequence.
      query_segment_pos: JTensor of shape [B, T]
      key_segment_pos: JTensor of shape [B, S]

    Returns:
      encoded: JTensor of shape [B, T, D].
      atten_probs: JTensor of shape [B, N, T, S].
    """
    p = self.hparams
    if p.combine_qkv:
      # Only supports self attention.
      assert query_vec is key_vec
      assert query_vec is value_vec
      # Project inputs to key, value and query using a combined weight for
      # faster performance on TPU.
      query_proj, key_proj, value_proj = self.combined_qkv(query_vec)
    else:
      # Project inputs to key, value and query, respectively has shape
      # [B, S, N, H], [B, S, N, H], and [B, T, N, H].
      query_proj = self.query(query_vec)
      key_proj = self.key(key_vec)
      value_proj = self.value(value_vec)

    self._fprop_update_decode_state('key_state', key_proj)
    self._fprop_update_decode_state('value_state', value_proj)

    # Apply depth-wise convolution as in Primer.
    # Paper: https://arxiv.org/abs/2109.08668.
    if p.dconv_qkv:
      self._fprop_update_decode_state('query_state', query_proj)
      query_proj = self.dconv_q(
          query_proj, axis=1, segment_pos=query_segment_pos)
      self._fprop_update_decode_state('query_post_dconv', query_proj)
      key_proj = self.dconv_k(key_proj, axis=1, segment_pos=key_segment_pos)
      self._fprop_update_decode_state('key_post_dconv', key_proj)
      value_proj = self.dconv_v(value_proj, axis=1, segment_pos=key_segment_pos)
      self._fprop_update_decode_state('value_post_dconv', value_proj)

    # Apply rotary position embeddings.
    # Paper: https://arxiv.org/abs/2104.09864.
    if p.use_rotary_position_emb:
      query_proj = self.rotary_position_emb(query_proj, query_segment_pos)
      key_proj = self.rotary_position_emb(key_proj, key_segment_pos)
      self._fprop_update_decode_state('key_post_rotary_pos_emb', key_proj)

    # Apply relative bias.
    # Paper: https://aclanthology.org/N18-2074.pdf.
    if p.relative_bias_tpl:
      relative_bias = self.relative_bias(query_segment_pos, key_segment_pos)
    else:
      relative_bias = None

    encoded, atten_probs = self._dot_atten(query_proj, key_proj, value_proj,
                                           atten_mask, relative_bias)

    # Apply NGrammer to the output of the attention layer.
    # Paper: https://openreview.net/forum?id=GxjCYmQAody.
    if p.ngrammer_tpl is not None:
      self._fprop_update_decode_state('encoded_pre_ngrammer', encoded)
      attention_scores = None
      if p.ngrammer_tpl.ngram_using_attention_scores:
        attention_scores = atten_probs
      encoded = self.ngrammer(
          input_ids=None,
          input_embs=encoded,
          segment_pos=key_segment_pos,
          merge_heads=False,
          attention_scores=attention_scores)

    # Post projection
    encoded = self.post(encoded)
    encoded = self._shard_bld(encoded)
    encoded = checkpoint_name(encoded, 'out_proj')

    return encoded, atten_probs

  def init_states(self, target_batch_size: int, target_max_length: int) -> None:
    """Initializes cache for autoregressive cached decoding.

    Args:
      target_batch_size: The batch size of the target to be decoded.
      target_max_length: The sequence length of the target to be decoded.
    Return: None.
    """
    raise NotImplementedError(type(self))

  @nn.nowrap
  def _fprop_update_decode_state(self, name: str, value: JTensor) -> None:
    """Updates decode state in fprop.

    This is a no-op in training.
    Args:
      name: Variable name in decoder cache.
      value: Value to extend at time step.
    """
    # Only update the state if it is decoding.
    if not self.is_mutable_collection(
        base_layer.DECODE_CACHE) or not self.hparams.decode_cache:
      return
    self.update_decode_state(name, value)

  @nn.nowrap
  def extend_decode_state(self, name: str, value: JTensor, time_step: JTensor,
                          time_dim: int) -> JTensor:
    """Extends decode state at time_step.

    The decode state is batch major with shape [B, T, N, H].
    Args:
      name: Variable name in decoder cache.
      value: Value to extend at time step.
      time_step: A scalar. Time step to update the state.
      time_dim: Time dimension in the decode state.

    Returns:
      Updated decode cache state of that variable.
    """
    extend_value = jnp.expand_dims(value, axis=time_dim)
    indices = [0] * extend_value.ndim
    indices[time_dim] = time_step.astype(jnp.int32)
    state = self.get_decode_state(name)
    assert state is not None
    new_state = jax.lax.dynamic_update_slice(state,
                                             extend_value.astype(state.dtype),
                                             indices)
    self.update_decode_state(name, new_state)
    return new_state

  def extend_step(self, query_vec: JTensor, *, atten_mask: JTensor,
                  time_step: JTensor,
                  segment_pos: Optional[JTensor]) -> JTensor:
    """Computes the value vector given the query of the current step.

    This function is used by autoregressive decoding.

    Args:
      query_vec: JTensor of shape [B, D] corresponding to query vector at index
        time_step.
      atten_mask: JTensor of shape [B/1, 1, S]. atten_mask should have already
        taken care of causal masking for decoding, plus other maskings
        necessary.
      time_step: A scalar or JTensor. Current time-step, 0-based.
      segment_pos: An optional JTensor of shape [B]. Current position in the
        same segment. If unspecified, time_step will be used.

    Returns:
      encoded: JTensor of shape [B, D] which returns the attention output at
        `time_step`.
    """
    p = self.hparams
    time_step = jnp.array(time_step)
    # Batch major.
    time_dim = 1
    assert time_step.ndim == 0
    if p.combine_qkv:
      # Project inputs to key, value and query using a combined weight for
      # faster performance on TPU.
      new_query_proj, new_key_proj, new_value_proj = self.combined_qkv(
          query_vec)
    else:
      # Project inputs to key, value and query. Each has shape [B, N, H].
      new_key_proj = self.key(query_vec)
      new_value_proj = self.value(query_vec)
      new_query_proj = self.query(query_vec)

    def _extend_decode_state_and_shard(name: str,
                                       extend_value: JTensor) -> JTensor:
      extended_state = self.extend_decode_state(
          name, extend_value, time_step, time_dim=time_dim)
      return self._shard_blnh(extended_state)

    # Update key_state
    key_state_name = 'key_state'
    key_state = _extend_decode_state_and_shard(key_state_name, new_key_proj)

    # Update value state.
    value_state_name = 'value_state'
    value_state = _extend_decode_state_and_shard(value_state_name,
                                                 new_value_proj)

    # Apply depth-wise convolution as in Primer.
    # Paper: https://arxiv.org/abs/2109.08668.
    if p.dconv_qkv:
      # Update query in cache.
      query_state = _extend_decode_state_and_shard('query_state',
                                                   new_query_proj)

      # Aggregate depth-wise convolution for keys and values at time step.
      new_query_proj = self.dconv_q.extend_step(
          query_state, axis=time_dim, step=time_step, segment_pos=segment_pos)
      new_key_proj = self.dconv_k.extend_step(
          key_state, axis=time_dim, step=time_step, segment_pos=segment_pos)
      new_value_proj = self.dconv_v.extend_step(
          value_state, axis=time_dim, step=time_step, segment_pos=segment_pos)

      # Update queries, keys and values post dconv in cache.

      query_state = _extend_decode_state_and_shard('query_post_dconv',
                                                   new_query_proj)
      key_state_name = 'key_post_dconv'
      key_state = _extend_decode_state_and_shard(key_state_name, new_key_proj)
      value_state_name = 'value_post_dconv'
      value_state = _extend_decode_state_and_shard(value_state_name,
                                                   new_value_proj)

    # Apply rotary position embeddings.
    # Paper: https://arxiv.org/abs/2104.09864.
    if p.use_rotary_position_emb:
      if segment_pos is None:
        position = jnp.broadcast_to(time_step, [query_vec.shape[0]])
      else:
        position = segment_pos
      new_query_proj = self.rotary_position_emb.extend_step(
          new_query_proj, position)
      new_key_proj = self.rotary_position_emb.extend_step(
          new_key_proj, position)

      # Update key post rotary position embedding in the cache.
      key_state_name = 'key_post_rotary_pos_emb'
      key_state = _extend_decode_state_and_shard(key_state_name, new_key_proj)

    if p.relative_bias_tpl:
      # Relative bias uses time_step instead of segment_pos.
      relative_bias = self.relative_bias.extend_step(
          seq_length=self.decoding_state_sequence_length(), time_step=time_step)
    else:
      relative_bias = None

    encoded, atten_prob = self._dot_atten_one_step(new_query_proj,
                                                   key_state_name,
                                                   value_state_name, atten_mask,
                                                   relative_bias)
    # TODO(yonghui): return atten_probs back to the caller.

    # Apply NGrammer to the output of the attention.
    # Paper: https://openreview.net/forum?id=GxjCYmQAody.
    if p.ngrammer_tpl is not None:
      encoded_state = _extend_decode_state_and_shard('encoded_pre_ngrammer',
                                                     encoded)
      # TODO(pax-dev): May need to fix segment_pos.
      attention_score = None
      if p.ngrammer_tpl.ngram_using_attention_scores:
        attention_score = atten_prob
      encoded = self.ngrammer.extend_step(
          encoded_state,
          step=time_step,
          merge_heads=False,
          attention_score=attention_score)

    del atten_prob
    # Post projection.
    encoded = self.post(encoded)
    encoded = self._shard_bd(encoded)
    return encoded

  def transform_decode_state(self,
                             transform_fn: base_layer.DecodeStateTransformFn):
    """Transforms all decode state variables based on transform_fn."""
    batch_dim = 0
    time_dim = 1
    for name, state in self.variables[base_layer.DECODE_CACHE].items():
      if not isinstance(state, JTensor):
        continue
      new_state = transform_fn(state, batch_dim, time_dim)
      self.update_decode_state(name, new_state)

  def lazy_broadcast_prefix(self, num_suffix_samples: int,
                            suffix_length: int) -> None:
    """Performs lazy prefix broadcast on the decoding states."""
    raise NotImplementedError(
        'lazy_broadcast_prefix not implemented, use DotProductAttentionWithLPB '
        'instead.')


# FnOnDecodeStateChunk is used for lazy prefix broadcast. See comments in
# DotProductAttentionWithLPB.
#
# A function that runs on a chunk of decoding states.
# fn(layer, args, args_to_slice, broadcast_args_to_slice, states)
# Args:
#   layer: a layer.
#   args: args with a batch dimension
#   args_to_slice: batched args, but need to be siced on the time dim for the
#     chunk.
#   broadcast_args_to_slice: args to be shared by all the batch samples, which
#     need to be siced on the time dim for the chunk.
#   states: a list of chunks for useful decoding states.
FnOnDecodeStateChunk = Callable[[
    base_layer
    .BaseLayer, NestedJTensor, NestedJTensor, NestedJTensor, Sequence[JTensor]
], NestedJTensor]


class DotProductAttentionWithLPB(DotProductAttention):
  """DotProductAttention with lazy prefix broadcast optimization for decoding.

  The "lazy prefix broadcast" technique separates decoding states of a shared
  prefix from decoding states of suffixes being generated from that prefix. It
  reduces memory usage and memory bandwidth for extend_step() for multi-sample
  generation by reading the prefix state once for all suffixes sharing it.

  When lazy_broadcast_prefix() is called, the current decoding state will be
  frozen, and moved from DECODE_CACHE to PREFIX_DECODE_CACHE. A new decoding
  state is created with a new num_suffix_samples dimension and a length. The
  logical sequence of a sample is the concatenation of the shared prefix and
  the suffix. lazy_broadcast_prefix() can be called multiple times, and each
  time all the previous prefixes will be marked lazy broadcast on an
  additional dimension. E.g., after two lazy_broadcast_prefix (the
  num_suffix_samples dim is set to 3 and 2), a decoding state (key_state)
  will have three chunks:

    key_state_0_pfx     key_state_1_pfx     key_state
     (chunk_id 0)        (chunk_id 1)      (chunk_id 2)
                                           [][][][][][]
                      [][][][][][][][][]   [][][][][][]
    [][][][][][][][]  [][][][][][][][][]   [][][][][][]
                      [][][][][][][][][]   [][][][][][]
                                           [][][][][][]
                                           [][][][][][]

  Self attention will be computed on these prefixes separately, then combined
  with the current state.

  Inputs to this layer will have a 6x larger batch dimension.

  To use this layer, replace the Transformer layer's attention template:
    lbp_tr_atten_tpl = attentions.DotProductAttentionWithLPB.HParams()
    if transformer_layer_p.tr_atten_tpl.cls == attentions.DotProductAttention:
      lbp_tr_atten_tpl.copy_fields_from(transformer_layer_p.tr_atten_tpl)
      transformer_layer_p.tr_atten_tpl = lbp_tr_atten_tpl
  """

  def _shard_blnh(self, x: JTensor) -> JTensor:
    """Adds sharding annotations to tensors of shape [b, l, n, h]."""
    p = self.hparams
    blnh = p.activation_split_dims_mapping.blnh
    if blnh is None:
      return x
    # It is possible that we added prefix-broadcast dimensions.
    blnh = [blnh[0]] + [None] * (x.ndim - 4) + list(blnh[1:])
    return base_layer.maybe_shard(x, blnh, p.mesh_axis_names)

  def lazy_broadcast_prefix(self, num_suffix_samples: int,
                            suffix_length: int) -> None:
    """Performs lazy prefix broadcast on the decoding states.

    Current decoding states will be moved to PREFIX_DECODE_CACHE. New decoding
    state will be created for the suffixes with multiple samples sharing
    previous prefixes. After this call, new extend_step will use a batch size
    num_suffix_samples times larger than before, which is logically 2 merged
    dimensions [previous batch dim, new num_samples dim].

    Args:
      num_suffix_samples: Number of samples that will share the same previous
        decoding state.
      suffix_length: The length of the new suffix samples.
    """
    prev_pfx_count = self._broadcast_prefixes_count

    for name, state in self.variables[base_layer.DECODE_CACHE].items():
      assert self.is_mutable_collection(PREFIX_DECODE_CACHE)
      self.put_variable(PREFIX_DECODE_CACHE, f'{name}_{prev_pfx_count}_pfx',
                        state)
      suffix_shape = state.shape[:prev_pfx_count + 1] + (
          num_suffix_samples, suffix_length) + state.shape[prev_pfx_count + 2:]
      self.update_decode_state(name, jnp.zeros(suffix_shape, dtype=state.dtype))

  @property
  def _broadcast_prefixes_count(self):
    """Returns the number of prefixes created for lazy broadcast."""
    if PREFIX_DECODE_CACHE not in self.variables:
      return 0
    count = 0
    while f'key_state_{count}_pfx' in self.variables[PREFIX_DECODE_CACHE]:
      count += 1
    return count

  def _broadcast_prefix_length(self):
    """Returns the sum of lengths of all lazy broadcast prefixes."""
    prefix_length = 0
    for i in range(self._broadcast_prefixes_count):
      prefix_length += self.get_variable(PREFIX_DECODE_CACHE,
                                         f'key_state_{i}_pfx').shape[i + 1]
    return prefix_length

  def decoding_state_sequence_length(self):
    """Returns the length of full decoding sequences including prefixes."""
    key_state_length = self.get_decode_state('key_state').shape[
        1 + self._broadcast_prefixes_count]
    return key_state_length + self._broadcast_prefix_length()

  def _vmap_on_broadcast_prefixes(self, fn: FnOnDecodeStateChunk, chunk_id: int,
                                  args_time_dims: NestedInt,
                                  broadcast_args_time_dims: NestedInt):
    """Transforms `fn` using vmap for a decoding state chunk."""

    # Wraps fn with slicing on args_to_slice and broadcast_args_to_slice.
    def _sliced_fn(layer, args, args_to_slice, broadcast_args_to_slice, states):
      sliced = jax.tree_map(
          lambda x, d: self._slice_decode_chunk(x, chunk_id, d), args_to_slice,
          args_time_dims)
      broadcast_sliced = jax.tree_map(
          lambda x, d: self._slice_decode_chunk(x, chunk_id, d),
          broadcast_args_to_slice, broadcast_args_time_dims)
      return fn(layer, args, sliced, broadcast_sliced, states)

    broadcast_dim_sizes = self.get_decode_state(
        'key_state').shape[1:1 + self._broadcast_prefixes_count]
    # There can be multiple lazy-broadcast sample dimensions, and we vmap one
    # dimension at a time. `args` and `args_to_slice` have shape
    # [b, num_samples0, num_samples1, ..., inner_dims]; after each vmap, one
    # num_samples dimension will be removed for `fn`.
    vfns = [_sliced_fn]
    # The loop works from inner vmap to outer vmap.
    for i in range(self._broadcast_prefixes_count):
      # args, args_to_slice have the sample dimensions. broadcast_args_to_slice
      # does not have them.
      in_axes = [i + 1, i + 1, None]
      if chunk_id > i:
        # This chunk has the current sample dimension to vmap. Since outer vmaps
        # (to be done at later iterations in this for loop) will handle sample
        # dimensions AFTER the current one, i + 1 is still the current vmap
        # even if there are outer vmaps. (1 in `i + 1` is the original batch
        # dim.)
        in_axes.append(i + 1)
      else:
        # This chunk does not have the current sample dimension to vmap.
        in_axes.append(None)
      # Do not vmap any state; they are handle explicitly as the `states`
      # argument in `fn`.
      vmapped_fn = nn.vmap(
          vfns[-1],
          variable_axes={
              base_layer.PARAMS: None,
              base_layer.DECODE_CACHE: None,
              base_layer.PREFIX_DECODE_CACHE: None,
          },
          in_axes=tuple(in_axes),
          out_axes=i + 1,
          split_rngs={
              base_layer.PARAMS: True,
              base_layer.RANDOM: True
          },
          axis_size=broadcast_dim_sizes[i])
      vfns.append(vmapped_fn)
    return vfns[-1]

  def _run_with_all_decode_state_chunks(
      self, fn: FnOnDecodeStateChunk, chunk_inputs: NestedJTensor,
      args_to_slice: NestedJTensor, args_time_dims: NestedInt,
      broadcast_args_to_slice: NestedJTensor,
      broadcast_args_time_dims: NestedInt, state_names: Sequence[str],
      combine_results: Callable[[Sequence[NestedJTensor]], NestedJTensor]
  ) -> NestedJTensor:
    """Runs `fn` on all decoding state chunks, then combine them."""
    pfx_count = self._broadcast_prefixes_count
    results = []
    for i in range(pfx_count + 1):
      # Get the relevant states for `fn`.
      if i == pfx_count:
        states = [self.get_decode_state(s) for s in state_names]
      else:
        states = [
            self.get_variable(PREFIX_DECODE_CACHE, f'{s}_{i}_pfx')
            for s in state_names
        ]
      # Run one chunk with vmaps.
      results.append(
          self._vmap_on_broadcast_prefixes(
              fn, i, args_time_dims,
              broadcast_args_time_dims)(self, chunk_inputs, args_to_slice,
                                        broadcast_args_to_slice, states))

    return combine_results(results)

  def _decode_state_chunk_length(self, chunk_id: int) -> int:
    """Returns the length of a decode state chunk (prefix or current)."""
    t_dim = chunk_id + 1
    if chunk_id == self._broadcast_prefixes_count:
      # Current state, non-prefix.
      return self.get_decode_state('key_state').shape[t_dim]
    return self.get_variable(PREFIX_DECODE_CACHE,
                             f'key_state_{chunk_id}_pfx').shape[t_dim]

  def _slice_decode_chunk(self, x: JTensor, chunk_id: int, dim: int) -> JTensor:
    """Slices a full-sequence tensor for a decode state chunk."""
    pfx_count = self._broadcast_prefixes_count
    start = 0
    for i in range(min(pfx_count, chunk_id)):
      t_dim = i + 1
      start += self.get_variable(PREFIX_DECODE_CACHE,
                                 f'key_state_{i}_pfx').shape[t_dim]
    limit = start + self._decode_state_chunk_length(chunk_id)
    return jax.lax.slice_in_dim(x, start, limit, axis=dim)

  def _left_concat_decode_state(self, state_name: str,
                                max_prefix_size: int) -> JTensor:
    """Left-concats the current decode state with prefixes (if any)."""
    state = self.get_decode_state(state_name)
    pfx_count = self._broadcast_prefixes_count
    if pfx_count == 0:
      return state
    batch_dims = self.get_decode_state(state_name).shape[:1 + pfx_count]
    windows = [state]
    prefix_window_size = max_prefix_size
    for i in range(pfx_count):
      if prefix_window_size == 0:
        break
      chunk_id = pfx_count - i - 1
      pfx = self.get_variable(PREFIX_DECODE_CACHE,
                              f'{state_name}_{chunk_id}_pfx')
      pfx_len = pfx.shape[chunk_id + 1]
      subwindow_len = min(pfx_len, prefix_window_size)
      prefix_window_size -= subwindow_len
      pfx = jax.lax.slice_in_dim(
          pfx, pfx_len - subwindow_len, pfx_len, axis=chunk_id + 1)
      pfx = jnp.reshape(
          pfx,
          batch_dims[:chunk_id + 1] + (1,) * (i + 1) + pfx.shape[chunk_id + 1:])
      pfx = jnp.broadcast_to(pfx, batch_dims + pfx.shape[len(batch_dims):])
      windows = [pfx] + windows
    return jnp.concatenate(windows, axis=pfx_count + 1)

  def _dot_atten_one_step(self,
                          query: JTensor,
                          key_state_name: str,
                          value_state_name: str,
                          atten_mask: JTensor,
                          relative_bias: Optional[JTensor] = None) -> JTensor:
    """Dot attention function for queries with 1 time step.

    In the shapes listed below, `...` means potential sample dims added for lazy
    broadcast prefixes.

    Args:
      query: JTensor of shape [B, ..., N, H].
      key_state_name: Name of the decoding key state variable.
      value_state_name: Name of the decoding value state variable.
      atten_mask: JTensor of shape [1/B, 1, S] which is a mask that is applied
        to prevent attention between unwanted pairs. This has already been
        converted into large negative logits. The first dimension is allowed to
        be of size 1, if the mask is shared by all items in the batch (e.g.,
        only a causal mask).
      relative_bias: Relative bias of shape [1/B, N, 1, S].

    Returns:
      encoded: JTensor of shape [B, ..., N, H].
    """

    p = self.hparams
    pfx_count = self._broadcast_prefixes_count

    batch_dims = self.get_decode_state(key_state_name).shape[:1 + pfx_count]
    rb_batched = False
    if relative_bias is not None:
      rb_batched = relative_bias.shape[0] > 1
    if rb_batched:
      relative_bias = jnp.reshape(relative_bias,
                                  batch_dims + relative_bias.shape[1:])
    am_batched = atten_mask.shape[0] > 1
    if am_batched:
      atten_mask = jnp.reshape(atten_mask, batch_dims + atten_mask.shape[1:])

    def _pre_softmax(layer, batched, batched_slice, non_batched_slice, states):
      del layer
      k = states[0]
      q = batched
      if am_batched:
        am, *batched_slice = batched_slice
      else:
        am, *non_batched_slice = non_batched_slice
      if rb_batched:
        rb, *batched_slice = batched_slice
      else:
        rb, *non_batched_slice = non_batched_slice
      k = self._shard_blnh(k)
      # q is 3d.
      q = self._shard_bnh(q)

      b, s, n, h = k.shape
      base_layer.assert_has_shape(q, [b, n, h])
      base_layer.assert_has_shape(am, [-1, 1, s])
      assert am.shape[0] in [1, b]
      q = self._scale_query(q)
      logits = jnp.einsum('BNH,BSNH->BNS', q, k)
      if rb is not None:
        base_layer.assert_has_shape(rb, [-1, n, 1, s])
        assert rb.shape[0] in [1, b]
        rb = jnp.squeeze(rb, axis=2)
        logits += rb
      logits = self._cap_logits(logits)
      # Attention softmax is always carried out in fp32.
      logits = logits.astype(jnp.float32)
      # Apply attention masking
      padded_logits = logits + am.astype(jnp.float32)
      return padded_logits

    batched_to_slice = []
    batched_to_slice_tdims = []
    non_batched_to_slice = []
    non_batched_to_slice_tdims = []
    if am_batched:
      batched_to_slice.append(atten_mask)
      batched_to_slice_tdims.append(2)
    else:
      non_batched_to_slice.append(atten_mask)
      non_batched_to_slice_tdims.append(2)
    if rb_batched:
      batched_to_slice.append(relative_bias)
      batched_to_slice_tdims.append(3)
    else:
      non_batched_to_slice.append(relative_bias)
      non_batched_to_slice_tdims.append(3)

    def _concat_logits(chunks):
      if len(chunks) == 1:
        return chunks[0]
      return jnp.concatenate(chunks, axis=pfx_count + 2)

    padded_logits = self._run_with_all_decode_state_chunks(
        _pre_softmax, query, batched_to_slice, batched_to_slice_tdims,
        non_batched_to_slice, non_batched_to_slice_tdims, [key_state_name],
        _concat_logits)

    # Of shape [b, ..., n, s]
    key_dtype = self.get_decode_state(key_state_name).dtype
    if p.attention_extra_logit is None:
      probs = jax.nn.softmax(padded_logits, axis=-1).astype(key_dtype)
    else:
      probs = jnp.exp(
          self._log_softmax_with_extra_logit(padded_logits)).astype(key_dtype)

    # Compute the attention context.
    def _post_softmax(layer, batched, ps, non_batched, states):
      del layer, batched, non_batched
      v = self._shard_blnh(states[0])
      return self._shard_bnh(jnp.einsum('BNS,BSNH->BNH', ps, v))

    # Use sum as result combiner since the time dimension is a contracting dim.
    encoded = self._run_with_all_decode_state_chunks(_post_softmax, [], probs,
                                                     2, [], [],
                                                     [value_state_name], sum)

    return encoded, probs

  def extend_step(self, query_vec: JTensor, *, atten_mask: JTensor,
                  time_step: JTensor,
                  segment_pos: Optional[JTensor]) -> JTensor:
    """Computes the value vector given the query of the current step.

    This function is used by autoregressive decoding.

    Args:
      query_vec: JTensor of shape [B, D] corresponding to query vector at index
        time_step.
      atten_mask: JTensor of shape [B/1, 1, S]. atten_mask should have already
        taken care of causal masking for decoding, plus other maskings
        necessary.
      time_step: A scalar or JTensor. Current time-step, 0-based.
      segment_pos: An optional JTensor of shape [B]. Current position in the
        same segment. If unspecified, time_step will be used.

    Returns:
      encoded: JTensor of shape [B, D] which returns the attention output at
        `time_step`.
    """
    p = self.hparams
    # Batch major. Reshape the input batch dim to match the decoding state if
    # there are lazy broadcast prefixes.
    pfx_count = self._broadcast_prefixes_count
    batch_dims = self.get_decode_state('key_state').shape[:1 + pfx_count]
    if pfx_count > 0:
      query_vec = jnp.reshape(query_vec, batch_dims + query_vec.shape[1:])
      if segment_pos is not None:
        segment_pos = jnp.reshape(segment_pos,
                                  batch_dims + segment_pos.shape[1:])

    time_step = jnp.array(time_step)
    assert time_step.ndim == 0

    # vmap a function on the samples dimensions in lazy broadcast prefixes. This
    # is for functions that do not touch the decoding states.
    def _vmap_no_state(fn):
      vfns = [fn]
      for i in range(pfx_count):
        vmapped_fn = nn.vmap(
            vfns[-1],
            variable_axes={
                base_layer.PARAMS: None,
                base_layer.DECODE_CACHE: None,
                base_layer.PREFIX_DECODE_CACHE: None,
            },
            in_axes=1,
            out_axes=1,
            split_rngs={
                base_layer.PARAMS: True,
                base_layer.RANDOM: True
            },
            axis_size=batch_dims[1 + i])
        vfns.append(vmapped_fn)
      return vfns[-1]

    def _proj_qkv(layer, q):
      if p.combine_qkv:
        # Project inputs to key, value and query using a combined weight for
        # faster performance on TPU.
        new_query_proj, new_key_proj, new_value_proj = layer.combined_qkv(q)
      else:
        # Project inputs to key, value and query. Each has shape [B, N, H].
        new_key_proj = layer.key(q)
        new_value_proj = layer.value(q)
        new_query_proj = layer.query(q)
      return new_query_proj, new_key_proj, new_value_proj

    new_query_proj, new_key_proj, new_value_proj = _vmap_no_state(_proj_qkv)(
        self, query_vec)
    prefix_length = self._broadcast_prefix_length()

    def _extend_decode_state_and_shard(name: str,
                                       extend_value: JTensor) -> JTensor:
      extended_state = self.extend_decode_state(
          name, extend_value, time_step - prefix_length, time_dim=1 + pfx_count)
      return self._shard_blnh(extended_state)

    # Update key_state
    key_state_name = 'key_state'
    _extend_decode_state_and_shard(key_state_name, new_key_proj)

    # Update value state.
    value_state_name = 'value_state'
    _extend_decode_state_and_shard(value_state_name, new_value_proj)

    # Apply depth-wise convolution as in Primer.
    # Paper: https://arxiv.org/abs/2109.08668.
    if p.dconv_qkv:
      # Update query in cache.
      _extend_decode_state_and_shard('query_state', new_query_proj)

      # For lazy prefix broadcast, we need to concat the current state with part
      # of prefixes to cover the dconv window.
      left_window_size = min(self.dconv_q.hparams.kernel_size - 1,
                             prefix_length)

      def _dconv(layer, q, k, v, pos):
        # Aggregate depth-wise convolution for keys and values at time step.
        t_dim = 1
        left_window_size = min(layer.dconv_q.hparams.kernel_size - 1,
                               prefix_length)
        ts = time_step - prefix_length + left_window_size
        new_query_proj = layer.dconv_q.extend_step(
            q, axis=t_dim, step=ts, segment_pos=pos)
        new_key_proj = layer.dconv_k.extend_step(
            k, axis=t_dim, step=ts, segment_pos=pos)
        new_value_proj = layer.dconv_v.extend_step(
            v, axis=t_dim, step=ts, segment_pos=pos)
        return new_query_proj, new_key_proj, new_value_proj

      new_query_proj, new_key_proj, new_value_proj = _vmap_no_state(_dconv)(
          self, self._left_concat_decode_state('query_state', left_window_size),
          self._left_concat_decode_state('key_state', left_window_size),
          self._left_concat_decode_state('value_state',
                                         left_window_size), segment_pos)

      # Update queries, keys and values post dconv in cache.
      _extend_decode_state_and_shard('query_post_dconv', new_query_proj)
      key_state_name = 'key_post_dconv'
      _extend_decode_state_and_shard(key_state_name, new_key_proj)
      value_state_name = 'value_post_dconv'
      _extend_decode_state_and_shard(value_state_name, new_value_proj)

    # Apply rotary position embeddings.
    # Paper: https://arxiv.org/abs/2104.09864.
    if p.use_rotary_position_emb:
      if segment_pos is None:
        position = jnp.broadcast_to(time_step, batch_dims)
      else:
        position = segment_pos

      def _rotary(layer, q, k, pos):
        new_query_proj = layer.rotary_position_emb.extend_step(q, pos)
        new_key_proj = layer.rotary_position_emb.extend_step(k, pos)
        return new_query_proj, new_key_proj

      new_query_proj, new_key_proj = _vmap_no_state(_rotary)(self,
                                                             new_query_proj,
                                                             new_key_proj,
                                                             position)

      # Update key post rotary position embedding in the cache.
      key_state_name = 'key_post_rotary_pos_emb'
      _extend_decode_state_and_shard(key_state_name, new_key_proj)

    if p.relative_bias_tpl:
      # Relative bias uses time_step instead of segment_pos.
      relative_bias = self.relative_bias.extend_step(
          seq_length=self.decoding_state_sequence_length(), time_step=time_step)
    else:
      relative_bias = None

    encoded, atten_prob = self._dot_atten_one_step(new_query_proj,
                                                   key_state_name,
                                                   value_state_name, atten_mask,
                                                   relative_bias)
    # TODO(yonghui): return atten_probs back to the caller.

    # Apply NGrammer to the output of the attention.
    # Paper: https://openreview.net/forum?id=GxjCYmQAody.
    if p.ngrammer_tpl is not None:
      if pfx_count > 0:
        raise NotImplementedError(
            'ngrammer does not yet support lazy prefix broadcast')
      encoded_state = _extend_decode_state_and_shard('encoded_pre_ngrammer',
                                                     encoded)
      # TODO(pax-dev): May need to fix segment_pos.
      encoded = self.ngrammer.extend_step(
          encoded_state, step=time_step, merge_heads=False)

    del atten_prob
    # Post projection.
    if pfx_count > 0:
      encoded = jnp.reshape(encoded, (-1,) + encoded.shape[1 + pfx_count:])
    encoded = self.post(encoded)
    encoded = self._shard_bd(encoded)
    return encoded


class DotProductAttentionXL(DotProductAttention):
  """Transformer-XL multiheaded attention with relative positional embedding.

  https://arxiv.org/pdf/1901.02860.pdf section 3.3.

  Notice this is only intended for self-attention.
  """

  class HParams(DotProductAttention.HParams):
    """Associated hyper-params for this layer class.

    Attributes:
      rel_pos_emb_dim: Dimension of relative positional embedding.
      skip_term_b: If True, skip term_b in the paper section 3.3.
    """
    rel_pos_emb_dim: int = 0
    skip_term_b: bool = False

  def setup(self) -> None:
    """Constructs a DotProductAttentionXL object."""
    super().setup()
    params = self.hparams
    wp = params.weight_split_dims_mapping

    if params.rel_pos_emb_dim <= 0:
      raise ValueError('Invalid rel_pos_emb_dim: %s' % params.rel_pos_emb_dim)

    emb_params = embedding_softmax.PositionalEmbedding.HParams(
        embedding_dims=params.rel_pos_emb_dim)
    self.create_child('pos_emb', emb_params)

    # Projection layer for relative position encoding
    pos_proj_tpl = params.proj_tpl.clone().set(
        input_dim=params.rel_pos_emb_dim,
        num_heads=params.num_heads,
        dim_per_head=params.dim_per_head,
        use_bias=False)
    pos_proj_tpl.weight_split_dims_mapping.wt = wp.proj
    self.create_child('pos_proj', pos_proj_tpl)

    u_pc = WeightHParams(
        shape=[params.num_heads, params.dim_per_head],
        init=WeightInit.Constant(0.0))
    v_pc = WeightHParams(
        shape=[params.num_heads, params.dim_per_head],
        init=WeightInit.Constant(0.0))

    self.create_variable('u', u_pc)
    self.create_variable('v', v_pc)

  def _rel_position_bias(self, content: JTensor,
                         abs_pos_emb: JTensor) -> JTensor:
    """Computes relative position bias.

    This is a subroutine used by variants of self-attentions with relative
    positional embedding.

    output[b][n][i][j] = content[b][i][n] x abs_pos_emb[i-j+T-1][n]

    Padding should be masked by the caller of this function.

    B: batch size
    T: sequence length
    N: num of attention heads.
    H: per-head attention dimension.

    Args:
      tensors of the following shapes:
      content:         [N, H] if skip_term_b else [B, T, N, H]
      abs_pos_emb:     [2T - 1, N, H], the absolute positional embedding.
        abs_pos_emb[i] is the emb of relative distance i - (T-1).

    Returns:
      The attention logits tensor. [N, T, T] if skip_term_b else [B, N, T, T].
    """
    params = self.hparams
    if not params.skip_term_b:
      b, t, n = content.shape[:3]
      l = 2 * t - 1

      # [B, N, T, L=2T-1]
      term_bd = jnp.einsum('BTNH,LNH->BNTL', content, abs_pos_emb)

      term_bd = jnp.reshape(term_bd, [b, n, t * l])
      # [B, N, T * (L + 1)].
      term_bd = jnp.pad(term_bd, ((0, 0), (0, 0), (0, t)))
      # [B, N, T, L + 1].
      term_bd = jnp.reshape(term_bd, [b, n, t, l + 1])
      return term_bd[:, :, :, t - 1::-1]
    else:
      n = content.shape[0]
      l = abs_pos_emb.shape[0]
      t = (l + 1) // 2

      # [N, L=2T-1]
      term_d = jnp.einsum('NH,LNH->NL', content, abs_pos_emb)

      # [N, T, L]
      term_d = jnp.tile(jnp.expand_dims(term_d, axis=1), [1, t, 1])
      term_d = jnp.reshape(term_d, [n, t * l])
      # [N, T * (L + 1)].
      term_d = jnp.pad(term_d, ((0, 0), (0, t)))
      # [N, T, L + 1].
      term_d = jnp.reshape(term_d, [n, t, l + 1])
      return term_d[:, :, t - 1::-1]

  def _atten_logits(self, query, key):
    p = self.hparams
    b, t, n, h = query.shape

    # This layer only supports self-attention.
    assert key.shape == (b, t, n, h)

    # [1, 2T - 1]
    pos = jnp.expand_dims(jnp.arange(-(t - 1), t), 0)
    sin_emb = self.pos_emb(position=pos)
    # [1, 2T - 1, N, H]
    sin_emb = self.pos_proj(sin_emb)
    # [2T - 1, N, H]
    sin_emb = jnp.squeeze(sin_emb, 0)

    # [B, N, T, S=T]
    content = query + self.theta.u
    term_ac = jnp.einsum('BTNH,BSNH->BNTS', content, key)

    if p.skip_term_b:
      content = self.theta.v
    else:
      content = query + self.theta.v
    term_bd = self._rel_position_bias(content, sin_emb)
    return term_ac + term_bd

  def _dot_atten_one_step(self,
                          query: JTensor,
                          key_state_name: str,
                          value_state_name: str,
                          atten_mask: JTensor,
                          relative_bias: Optional[JTensor] = None) -> JTensor:
    raise NotImplementedError('One step is not implemented for %s' %
                              self.__name__)

  def init_states(self, target_batch_size: int,
                  target_max_length: int) -> NestedMap:

    raise NotImplementedError('init_states is not implemented for %s' %
                              self.__name__)

  def extend_step(self, cached_states: NestedMap, query_vec: JTensor, *,
                  atten_mask: JTensor,
                  time_step: JTensor) -> Tuple[JTensor, NestedMap]:
    raise NotImplementedError('extend_step is not implemented for %s' %
                              self.__name__)


class LocalSelfAttention(DotProductAttention):
  """Local Attention with given left and right context.

  We use the following capital letters to denote certain
  tensor parameters.

    B = batch size.
    P = query stride (default to 1, see below).
    T(target) = length of the query.
    S(source) = length of the key/value, S == T * P.

    W = key block size. query block size is W // P.
    L = left context size in key, including left L-1 positions and self.
    R = right context size in key.
    F = L + R = context size of one position.
    C = L + R + W - 1 = context size of a block of W positions.
    U = ceiling(T/W).

    D = model dimension.
    N = number of attention heads.
    H = dimensions of each attention head.

  Canonical attention:
  For each query position, its attended position range in the key sequence
  includes from the left L-1 tokens before it (up to the beginning of the
  sequence), the self, and the right R tokens after it (up to the end of the
  sequence). This is not affected by the block size.

  Causality is enabled when right context size R=0.

  The key difference to base class is on calculating logits:
    Base class:
      1)  Compute the full S x T attention.
      2)  Apply a S x T mask to enforce local attention window.
    This implementation:
      1)  Compute a W x C attention for each of the U blocks. Where the i-th
      block has query[W*i:W*(i+1)] and key[W*(i-1)-L-1:W*(i+1)+R].
      2)  Apply a W x C mask for each block.

  Effectively, we reduce both time and space complexities for computing the
  sliding window attention from O(S * T) to O(S * C). In practice we observe
  reduced HBM usage on TPU but no speed gains.

  Strided attention:
  For canonical attention, P is 1 and S == T. When query_stride (P) is not 1,
  query(target) and key/value(source) have different lengths: S is expected
  to be a multiple T.

  The attention semantics also change, in that, position i in the query will
  attend to the same range in the key sequence as covered by [i, i+P) in
  the canonical attention.

  Note: Key and query need to have the same length. Ideally one can support
  cross attention. So far this class is only used for encoder in speech models.
  """

  class HParams(DotProductAttention.HParams):
    """Associated hyper-params for this layer class.

    Attributes:
      block_size: Size of a processing block, if unset, default to max(1,
        left_context-1).
      left_context: Number of left positions to attend (including current
        position).
      right_context: Number of right positions to attend.
    """
    block_size: Optional[int] = None
    left_context: Optional[int] = None
    right_context: int = 0

  def _atten_logits(self, query: JTensor, key: JTensor) -> JTensor:
    """Compute logits from query and key."""
    logits = jnp.einsum('buwnh,bucnh->bnuwc', query, key)
    return logits

  def _dot_atten(
      self,
      query: JTensor,
      key: JTensor,
      value: JTensor,
      atten_mask: JTensor,
      relative_bias: Optional[JTensor] = None) -> Tuple[JTensor, JTensor]:
    """Main attention function.

    Args:
      query: JTensor of shape [B, T, N, H].
      key: JTensor of shape [B, S, N, H].
      value: JTensor of shape [B, S, N, H].
      atten_mask: JTensor of shape [1/B, 1, 1/T, S] which is a mask that is
        applied to prevent attention between unwanted pairs. This has already
        been converted into large negative logits. Note that the first and third
        dimension allow size 1 if the mask is shared by every item in the batch
        or every token in the target sequence.
      relative_bias: Relative bias of shape [B, N, T, S].

    Returns:
      encoded: JTensor of shape [B, T, N, H].
      atten_probs: JTensor of shape [B, N, T, S].
    """
    # Relative bias is not supported yet
    if relative_bias is not None:
      raise NotImplementedError(
          'relative bias for localattention is not supported yet')
    # Add key sharding annotations.
    p = self.hparams
    query = self._shard_blnh(query)
    key = self._shard_blnh(key)
    value = self._shard_blnh(value)

    b, s, n, h = key.shape
    base_layer.assert_has_shape(value, [b, s, n, h])
    base_layer.assert_has_shape(query, [b, -1, n, h])
    t = query.shape[1]
    # If only padding bias is supplied, then atten_mask can be [B, 1, 1, S]
    # since each target token is prohibited from attending to the same set of
    # source tokens. In this case tiling is inefficient and unnecessary.
    # If there is no padding mask, and only causal mask then the shape can be
    # [1, 1, T, S]
    base_layer.assert_has_shape(atten_mask, [-1, 1, -1, s])
    assert atten_mask.shape[2] in [1, t]
    assert atten_mask.shape[0] in [1, b]
    query = self._scale_query(query)

    # -> [B, U, C, N, H]
    key_block_context = _extract_block_context(
        key,
        block_size=p.block_size,
        left_context=p.left_context,
        right_context=p.right_context)
    _, u, c, _, _ = key_block_context.shape

    # -> [B, U, W, N, H]
    query_blocks = _convert_to_block(query, block_size=p.block_size)
    _, _, w, _, _ = query_blocks.shape

    minus_inf = py_utils.get_large_negative_number(jnp.float32)

    if atten_mask.shape[2] == 1:
      # Attention mask with shape [1/B, 1, 1, S]
      # For example, generated by convert_paddings_to_mask

      mask = atten_mask[:, 0, 0, :]
      mask_block_context = _extract_block_context(
          mask,
          block_size=p.block_size,
          left_context=p.left_context,
          right_context=p.right_context,
          padding_val=minus_inf)

      # -> [B, N, U, W, C]
      mask = jnp.tile(
          jnp.reshape(mask_block_context, [b, 1, u, 1, c]), [1, n, 1, w, 1])
    else:
      # Full attention mask

      # -> [B, U, W, T]
      mask_block_context = _convert_to_block(
          atten_mask[:, 0].astype(jnp.float32),
          block_size=p.block_size,
          padding_val=minus_inf)
      mask_block_context = jnp.reshape(mask_block_context, [b * u * w, t])
      # -> [B, U, W, U, C]
      mask_block_context = _extract_block_context(
          mask_block_context,
          block_size=p.block_size,
          left_context=p.left_context,
          right_context=p.right_context,
          padding_val=minus_inf)
      mask_block_context = jnp.reshape(mask_block_context, [b, u, w, u, c])
      mask_block_context = jnp.einsum('buwuc->buwc', mask_block_context)

      # -> [B, N, U, W, C]
      mask = jnp.tile(jnp.expand_dims(mask_block_context, 1), [1, n, 1, 1, 1])
      assert mask.shape == (b, n, u, w, c)

    # Make local causal mask.
    # -> [U, W, C]
    local_causal_mask = _make_local_mask(
        seq_len=t,
        block_size=p.block_size,
        left_context=p.left_context,
        right_context=p.right_context)
    mask = jnp.minimum(mask, (1. - local_causal_mask) * minus_inf)

    # -> [B, N, U, W, C]
    logits = self._atten_logits(query_blocks, key_block_context)
    logits = checkpoint_name(logits, 'logits')
    logits = self._cap_logits(logits)
    # Attention softmax is always carried out in fp32.
    logits = logits.astype(jnp.float32)

    padded_logits = logits + mask.astype(jnp.float32)

    if p.attention_extra_logit is None:
      probs = jax.nn.softmax(padded_logits, axis=-1).astype(key.dtype)
    else:
      probs = jnp.exp(self._log_softmax_with_extra_logit(padded_logits)).astype(
          key.dtype)
    # Apply attention dropout.
    probs = self.atten_dropout(probs)

    value_block_context = _extract_block_context(
        value,
        block_size=p.block_size,
        left_context=p.left_context,
        right_context=p.right_context)

    # Compute the attention context vector.
    # -> [B, U, W, N, H]
    encoded = jnp.einsum('bnuwc,bucnh->buwnh', probs, value_block_context)
    encoded = jnp.reshape(encoded, [b, u * w, n, h])
    # Remove the extra time padding introduced by converting to blocks.
    encoded = encoded[:, :query.shape[1], ...]

    encoded = checkpoint_name(encoded, 'context')
    encoded = self._shard_blnh(encoded)
    return encoded, probs

  def _dot_atten_one_step(self,
                          query: JTensor,
                          key_state_name: str,
                          value_state_name: str,
                          atten_mask: JTensor,
                          relative_bias: Optional[JTensor] = None) -> JTensor:
    raise NotImplementedError('One step is not implemented for %s' %
                              self.__name__)

  def init_states(self, target_batch_size: int,
                  target_max_length: int) -> NestedMap:

    raise NotImplementedError('init_states is not implemented for %s' %
                              self.__name__)

  def extend_step(self, cached_states: NestedMap, query_vec: JTensor, *,
                  atten_mask: JTensor,
                  time_step: JTensor) -> Tuple[JTensor, NestedMap]:
    raise NotImplementedError('extend_step is not implemented for %s' %
                              self.__name__)


class LocalSelfAttentionXL(LocalSelfAttention):
  """Local causal version of transformer-xl self attention."""

  class HParams(LocalSelfAttention.HParams):
    """Associated hyper-params for this layer class.

    Attributes:
      rel_pos_emb_dim: Dimension of relative positional embedding.
      skip_term_b: bool. If True, skip term_b in the paper section 3.3.
    """
    rel_pos_emb_dim: int = 0
    skip_term_b: bool = False

  def setup(self) -> None:
    """Constructs a LocalSelfAttentionXL object."""
    super().setup()
    params = self.hparams
    wp = params.weight_split_dims_mapping

    if params.rel_pos_emb_dim <= 0:
      raise ValueError('Invalid rel_pos_emb_dim: %s' % params.rel_pos_emb_dim)

    emb_params = embedding_softmax.PositionalEmbedding.HParams(
        embedding_dims=params.rel_pos_emb_dim)
    self.create_child('pos_emb', emb_params)

    # Projection layer for relative position encoding
    pos_proj_tpl = params.proj_tpl.clone().set(
        input_dim=params.rel_pos_emb_dim,
        num_heads=params.num_heads,
        dim_per_head=params.dim_per_head,
        use_bias=False)
    pos_proj_tpl.weight_split_dims_mapping.wt = wp.proj
    self.create_child('pos_proj', pos_proj_tpl)

    u_pc = WeightHParams(
        shape=[params.num_heads, params.dim_per_head],
        init=WeightInit.Constant(0.0))
    v_pc = WeightHParams(
        shape=[params.num_heads, params.dim_per_head],
        init=WeightInit.Constant(0.0))

    self.create_variable('u', u_pc)
    self.create_variable('v', v_pc)

  def _atten_logits(self, query, key):
    p = self.hparams

    b, u, w = query.shape[:3]
    c = key.shape[2]
    n = p.num_heads
    l = p.left_context
    r = p.right_context
    f = l + r
    # term a and c
    term_ac = jnp.einsum('BUWNH,BUCNH->BNUWC', query + self.theta.u, key)

    # term b and d
    # [1, F]
    pos = jnp.expand_dims(jnp.arange(l - 1, -r - 1, -1), 0)
    sin_emb = self.pos_emb(position=pos)
    # [1, F, N, H]
    sin_emb = self.pos_proj(sin_emb)
    # [F, N, H]
    sin_emb = jnp.squeeze(sin_emb, 0)

    if not p.skip_term_b:
      # [B, N, U, W, F]
      term_bd = jnp.einsum('BUWNH,FNH->BNUWF', query + self.theta.v, sin_emb)

      # Perform relative shift in order to get [B, N, U, W, C]
      # Pads the input to [B, N, U, C, C+1]
      term_bd = jnp.pad(term_bd,
                        ((0, 0), (0, 0), (0, 0), (0, c - w), (0, c + 1 - f)))

      # Reshapes to [B, N, U, C+1, C]. Note the output last dim is 1-smaller
      # than the input, which "pushses" one element off to the next row for each
      # row. The accumulated effect is row_i is right-shifted i steps (i>=0).
      term_bd = jnp.reshape(term_bd, [b, n, u, c + 1, c])

      # Keeps useful slices. [B, N, U, W, C]
      term_bd = term_bd[:, :, :, :w, :]
    else:
      # [N, F]
      term_d = jnp.einsum('NH,FNH->NF', self.theta.v, sin_emb)
      # [N, W, F]
      term_d = jax.numpy.tile(jnp.expand_dims(term_d, 1), [1, w, 1])
      # [N, C, C+1]
      term_d = jnp.pad(term_d, ((0, 0), (0, c - w), (0, c + 1 - f)))
      # [N, C+1, C]
      term_d = jnp.reshape(term_d, [n, c + 1, c])
      # Keeps useful slices. [N, W, C]
      term_d = term_d[:, :w, :]
      term_bd = jnp.reshape(term_d, [1, n, 1, w, c])
    return term_ac + term_bd


class ChunkedCrossAttention(base_layer.BaseLayer):
  """Chunked cross attention used in the RETRO paper.

  This implementation is based on https://arxiv.org/abs/2112.04426 Section 2.4
  and B.1.3 (page 27).
  """

  class HParams(BaseHParams):
    """HParams.

    In addition to dims used in DotProductAttention, we assume:

    M: chunk length.
    L: number of chunks in the given query sequence. T = L x M.
    K: number of neighbors retrieved.
    R: length of neighbors retrieved.

    Our target sequence length (query) has sequence length T, which should
    equal to L x M. This is denotes as N in the paper but our convention here
    in this file uses N for number of attention heads. We instead follow the
    convention to use T = L x M to denote the sequence length of the query.

    Attributes:
      atten: an instance of DotProductAttention's HParams.
    """
    atten: BaseHParams = sub_config_field(DotProductAttention.HParams)

  def setup(self):
    if self.hparams.atten.relative_bias_tpl is None:
      raise ValueError('Must specify p.atten.relative_bias_tpl')
    if self.hparams.atten.relative_bias_tpl.use_length_as_position:
      # Note: A future optimization here is to not use the batch dimension in
      # relative_bias, since we have the same result for each row in the batch,
      # which would have the same memory saving as use_length_as_position=True.
      raise ValueError('Must have p.atten.relative_bias_tpl.'
                       'use_length_as_position=False, as we rely on '
                       '`key_segment_pos` for correctness.')
    self.create_child('atten', self.hparams.atten)

  def _cross_atten(self, chunk: JTensor, neighbors: JTensor) -> JTensor:
    """Computes cross attention between one chunk and its neighbors.

    Args:
      chunk: shape [B, M, D].
      neighbors: shape [B, K, R, D].

    Returns:
      Encoded output, shape [B, M, D].
    """
    # We assemble the key/value from `neighbors` with the right shape
    # [B, K * R, D] and its corresponding key_segment_pos [B, K * R].
    # and use self.atten.fprop to compute this cross attention.
    b, m, d = chunk.shape
    base_layer.assert_has_shape(neighbors, [b, -1, -1, d])
    _, k, r, d = neighbors.shape
    # => [k, r]
    key_pos = np.arange(r, dtype=jnp.int32)[None, :]
    key_pos = jnp.tile(key_pos, [k, 1])
    # => [b, k * r]
    key_segment_pos = jnp.reshape(key_pos, [1, k * r])
    key_segment_pos = jnp.tile(key_segment_pos, [b, 1])
    # [b, k * r, d]
    key_vec = jnp.reshape(neighbors, [b, k * r, d])
    atten_mask = jnp.zeros([b, 1, 1, k * r], dtype=chunk.dtype)
    # [b, m]
    query_segment_pos = np.arange(m, dtype=jnp.int32)[None, :]
    query_segment_pos = jnp.tile(query_segment_pos, [b, 1])
    encoded, _ = self.atten(
        query_vec=chunk,
        key_vec=key_vec,
        value_vec=key_vec,
        atten_mask=atten_mask,
        query_segment_pos=query_segment_pos,
        key_segment_pos=key_segment_pos)
    # [b, m, d]
    return encoded

  def __call__(self, query: JTensor, neighbors: JTensor) -> JTensor:
    """Computes the chunked cross attention.

    Not supported: packed input; decoding with extend_step.

    Note that we do not take query_paddings as an arg. Caller should ignore
    the output at padded positions on query. We assume there are no padded
    positions on `neighbors` and attend to all of its positions.

    Args:
      query: [B, T, D]. Caller must pad to integral multiples of chunk length,
        i.e. ensure that T == L * M, where M is the chunk length.
      neighbors: [B, L, K, R, D]. Retrieved neighbors to attend to.

    Returns:
      Encoded output of shape [B, T, D].
    """
    b, t, d = query.shape
    base_layer.assert_has_shape(neighbors, [b, -1, -1, -1, d])
    b, l, k, r, d = neighbors.shape

    if t % l:
      raise ValueError('Expecting sequence length to be multiples of chunk '
                       f'length, got seq len={t}, num chunks={l}')
    m = t // l
    # [B, L, M, D], but left shifted by (M-1).
    # We shift by (M-1) because the first chunk's retrieved neighbors
    # corresponds to position [0, M-1] in query, so position (M-1) is the first
    # position eligible to attend to this chunk without breaking causality.
    attending_chunks = jnp.pad(
        query[:, m - 1:, :], ((0, 0), (0, m - 1), (0, 0)),
        mode='constant').reshape(b, l, m, d)
    # shape [B, L, M, D]
    # Note that we avoid using jnp.vectorize here as it has a side effect of
    # expanding dim by 1 when going through the attention layer, and would
    # cause tracing failure.
    chunked_output = self._cross_atten(
        jnp.reshape(attending_chunks, (b * l, m, d)),
        jnp.reshape(neighbors, (b * l, k, r, d)))

    # shape [B, T, D]
    chunked_output = chunked_output.reshape(b, l * m, d)
    # shape [B, T + (M-1), D]
    # The first (M-1) positions are identity.
    joined_output = jnp.concatenate([query[:, :m - 1, :], chunked_output],
                                    axis=1)
    # shape [B, T, D]
    output = joined_output[:, :t, :]
    return output


class CausalDepthwiseConv1D(base_layer.BaseLayer):
  """Causal depth-wise convolution applied to a 1-d sequence as in Primer.

  See https://arxiv.org/abs/2109.08668 for more details.
  """

  class HParams(BaseHParams):
    """Associated hyper-params for this layer class.

    Attributes:
      kernel_size: Kernel size for the causal depth-wise convolution on the 1-D
        sequence.
      hidden_dims: Dimensions of the convolution filter. It can be a list to
        signify if we convolve multiple dimensions from the end of the sequence.
        Alternatively, if just convolving over the last dimension, it can be a
        positive integer.
    """
    kernel_size: int = 3
    hidden_dims: Union[int, Sequence[int]] = 0

  def setup(self) -> None:
    p = self.hparams
    assert p.name
    assert isinstance(p.hidden_dims, list) or isinstance(p.hidden_dims, int)
    assert p.kernel_size > 0
    if isinstance(p.hidden_dims, list):
      for dim in p.hidden_dims:
        assert dim > 0
    else:
      assert p.hidden_dims > 0

    wp = p.weight_split_dims_mapping
    for i in range(p.kernel_size):
      if i == 0:
        params_init = base_layer.WeightInit.Constant(0.5)
      else:
        params_init = base_layer.WeightInit.Constant(0.5 / p.kernel_size)
      if isinstance(p.hidden_dims, list):
        shape = p.hidden_dims
      else:
        shape = [p.hidden_dims]
      self.create_variable(
          f'dconv_{i}',
          WeightHParams(
              shape=shape,
              init=params_init,
              mesh_shape=p.mesh_shape,
              tensor_split_dims_mapping=wp.wt))

  def __call__(self,
               inputs: JTensor,
               axis: int,
               segment_pos: Optional[JTensor] = None) -> JTensor:
    """FProp applying depth-wise convolution on 1D sequence.

    Args:
      inputs: Input sequence of possible shapes: [B, L, D], [B, L, N, H] or [L,
        B, N, H] where the L represents the sequence length.
      axis: The axis which corresponds to the sequence dimension, i.e. the
        dimension corresponding to L. By default the axis is assumed to be 1.
      segment_pos: JTensor of shape [B, L].

    Returns:
      Output sequence after applying the depth-wise convolution on the sequence.
    """
    p = self.hparams
    outputs = inputs * self.theta.dconv_0
    for i in range(1, p.kernel_size):
      inputs = shift_1d(inputs, offset=1, axis=axis)
      if segment_pos is None:
        outputs += inputs * getattr(self.theta, f'dconv_{i}')
      else:
        mask = segment_pos >= i
        while len(mask.shape) < len(inputs.shape):
          mask = jnp.expand_dims(mask, axis=-1)
        outputs += inputs * getattr(self.theta, f'dconv_{i}') * mask
    return outputs

  def extend_step(self, inputs: JTensor, axis: int, step: Union[int, JTensor],
                  segment_pos: Optional[JTensor]) -> JTensor:
    """extend_step applying depth-wise convolution on 1D sequence at a step.

    Args:
      inputs: Input sequence of possible shapes: [B, L, D], [B, L, N, H] or [L,
        B, N, H] where the L represents the sequence length.
      axis: The axis which corresponds to the sequence dimension, i.e. the
        dimension corresponding to L. By default the axis is assumed to be 1.
      step: Which step to perform the convolution for. This must be a valid
        non-negative index into the length dimension L.
      segment_pos: JTensor of shape [B]. If not provided, it uses step as
        segment_pos.

    Returns:
      Output sequence at the step after applying the depth-wise convolution
      on the sequence.
    """
    p = self.hparams
    get_single_slice_at_index = functools.partial(
        jax.lax.dynamic_slice_in_dim, inputs, slice_size=1, axis=axis)
    outputs = get_single_slice_at_index(start_index=step)
    outputs *= self.theta.dconv_0
    if segment_pos is None:
      segment_pos = step
    else:
      new_shape = [segment_pos.shape[0]] + [1] * (inputs.ndim - 1)
      segment_pos = jnp.reshape(segment_pos, new_shape)
    use_where = not isinstance(segment_pos, int)
    for i in range(1, p.kernel_size):
      if use_where:
        prev_slice = jnp.where(
            jnp.greater_equal(segment_pos - i, 0),
            get_single_slice_at_index(step - i), jnp.zeros_like(outputs))
      elif segment_pos >= i:
        prev_slice = get_single_slice_at_index(start_index=step - i)
      else:
        break
      outputs += prev_slice * getattr(self.theta, f'dconv_{i}')
    return jnp.squeeze(outputs, axis)
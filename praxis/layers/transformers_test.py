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

"""Tests for Praxis transformer layers."""

import copy
import dataclasses
import itertools

from absl import logging
from absl.testing import absltest
from absl.testing import parameterized
import jax
from jax import numpy as jnp
from lingvo.core import batch_major_attention
from lingvo.core import layers_with_attention
import numpy as np
from praxis import base_hyperparams
from praxis import base_layer
from praxis import py_utils
from praxis import test_utils
from praxis.layers import attentions
from praxis.layers import transformers
import tensorflow.compat.v2 as tf

PARAMS = base_layer.PARAMS
RANDOM = base_layer.RANDOM
DECODE_CACHE = base_layer.DECODE_CACHE
instantiate = base_hyperparams.instantiate


class TransformersTest(test_utils.TestCase):

  def setUp(self):
    super().setUp()
    np.random.seed(123456)
    tf.random.set_seed(123)

  @parameterized.parameters(*list(itertools.product([True, False], repeat=3)))
  def test_transformer_layer(self, mask_self_attention, packed_input,
                             cross_attention):
    p = transformers.Transformer.HParams(
        name='jax_transformer_layer',
        input_dims=32,
        hidden_dims=128,
        num_heads=8,
        mask_self_attention=mask_self_attention,
        packed_input=packed_input,
        cross_attention=cross_attention)
    seq_len = np.random.randint(10, 32)
    batch_size = 10
    transformer_layer = instantiate(p)
    prng_key = jax.random.PRNGKey(seed=123)
    initial_vars = transformer_layer.init(prng_key)
    npy_inputs = np.random.normal(
        1.0, 0.5, [batch_size, seq_len, p.input_dims]).astype('float32')
    inputs = jnp.asarray(npy_inputs)
    npy_paddings = np.random.randint(0, 1,
                                     [batch_size, seq_len]).astype('float32')
    paddings = jnp.asarray(npy_paddings)
    causal_mask = None
    segment_mask = None
    tf_segment_mask = None
    attention_mask = attentions.convert_paddings_to_mask(paddings)
    if mask_self_attention:
      causal_mask = attentions.causal_mask(inputs)
      attention_mask = jnp.minimum(attention_mask, causal_mask)
    if packed_input:
      segment_ids = np.random.randint(0, 3, [batch_size, seq_len])
      segment_mask = attentions.segment_mask(segment_ids, dtype=np.float32)
      attention_mask = jnp.minimum(attention_mask, segment_mask)
      if mask_self_attention:
        tf_segment_mask = batch_major_attention.CausalSegmentMask(
            segment_ids, tf.float32)
      else:
        tf_segment_mask = batch_major_attention.SegmentMask(
            segment_ids, segment_ids)

    cross_inputs = None
    cross_attention_mask = None
    tf_cross_inputs = None
    tf_cross_paddings = None
    tf_cross_segment_mask = None
    if cross_attention:
      cross_seq_len = np.random.randint(10, 128)
      npy_cross_inputs = np.random.normal(
          1.0, 0.5, [batch_size, cross_seq_len, p.input_dims]).astype('float32')
      cross_inputs = jnp.asarray(npy_cross_inputs)
      tf_cross_inputs = tf.constant(npy_cross_inputs, dtype=tf.float32)
      npy_cross_paddings = np.random.randint(
          0, 1, [batch_size, cross_seq_len]).astype('float32')
      cross_paddings = jnp.asarray(npy_cross_paddings)
      cross_attention_mask = attentions.convert_paddings_to_mask(cross_paddings)
      tf_cross_paddings = tf.constant(npy_cross_paddings, dtype=tf.float32)
      if packed_input:
        source_segment_ids = np.random.randint(0, 3,
                                               [batch_size, cross_seq_len])
        cross_segment_mask = attentions.segment_mask(
            segment_ids, source_segment_ids, dtype=np.float32)
        cross_attention_mask = jnp.minimum(cross_attention_mask,
                                           cross_segment_mask)
        tf_cross_segment_mask = batch_major_attention.SegmentMask(
            segment_ids, source_segment_ids)

    with base_layer.JaxContext.new_context():
      outputs, unused_atten_probs = transformer_layer.apply(
          initial_vars,
          inputs,
          paddings,
          attention_mask=attention_mask,
          cross_inputs=cross_inputs,
          cross_attention_mask=cross_attention_mask)
    logging.info('initial_vars in transformer layer = %s', initial_vars)

    # Test whether tf Transformer layer returns same output
    # Modify initial_vars to use TF compatible params
    tf_initial_vars = py_utils.NestedMap.FromNestedDict(initial_vars[PARAMS])
    tf_initial_vars = test_utils.replace_jax_attention_vars_to_tf(
        tf_initial_vars, cross_attention)
    tf_initial_vars = test_utils.to_tf_nmap(tf_initial_vars)
    logging.info('tf_initial_vars in transformer layer = %s', tf_initial_vars)
    tf_p = batch_major_attention.TransformerLayer.Params().Set(
        name='tf_transformer_layer',
        input_dim=p.input_dims,
        num_heads=p.num_heads,
        mask_self_atten=mask_self_attention,
        packed_input=packed_input,
        has_aux_atten=cross_attention)
    tf_p.tr_fflayer_tpl.hidden_dim = p.hidden_dims
    tf_p.tr_fflayer_tpl.fflayer_tpl.batch_norm = False
    tf_p.tr_fflayer_tpl.fflayer_tpl.has_bias = True
    tf_transformer_layer = tf_p.Instantiate()
    if tf_cross_segment_mask is not None:
      tf_cross_segment_mask = test_utils.to_tf_nmap(tf_cross_segment_mask)
    tf_outputs, _ = tf_transformer_layer.FProp(
        tf_initial_vars,
        tf.constant(npy_inputs, dtype=tf.float32),
        paddings=test_utils.to_tf_nmap(npy_paddings),
        segment_mask=tf_segment_mask,
        aux_vec=tf_cross_inputs,
        aux_paddings=tf_cross_paddings,
        aux_segment_mask=tf_cross_segment_mask)
    np_outputs = test_utils.to_np(outputs)
    tf_np_outputs = test_utils.to_np(tf_outputs)
    self.assertAllClose(tf_np_outputs, np_outputs, atol=1e-5)

  @parameterized.parameters(*list(itertools.product([True, False], repeat=4)))
  def test_transformer_layer_extendstep(self, packed_input, cross_attention,
                                        dconv_qkv, use_rotary_position_emb):
    p = transformers.Transformer.HParams(
        name='jax_transformer_layer',
        input_dims=8,
        hidden_dims=32,
        num_heads=4,
        mask_self_attention=True,
        packed_input=packed_input,
        cross_attention=cross_attention)
    p.tr_atten_tpl.dconv_qkv = dconv_qkv
    p.tr_atten_tpl.use_rotary_position_emb = use_rotary_position_emb
    if cross_attention:
      p.cross_atten_tpl = copy.deepcopy(p.tr_atten_tpl)
      # Cross attention should not have depth-wise convolution.
      p.cross_atten_tpl.dconv_qkv = False
      # Cross attention should not have rotary position embedding.
      p.cross_atten_tpl.use_rotary_position_emb = False

    p.tr_atten_tpl.dconv_kernel_size = 2
    seq_len = 4
    batch_size = 4
    transformer_layer = instantiate(p)
    prng_key = jax.random.PRNGKey(seed=123)
    initial_vars = transformer_layer.init(prng_key)
    npy_inputs = np.random.normal(
        1.0, 0.5, [batch_size, seq_len, p.input_dims]).astype('float32')
    inputs = jnp.asarray(npy_inputs)
    npy_paddings = np.random.randint(0, 1,
                                     [batch_size, seq_len]).astype('float32')
    # npy_paddings = np.zeros([batch_size, seq_len])
    paddings = jnp.asarray(npy_paddings)
    attention_mask = attentions.convert_paddings_to_mask(paddings)
    segment_mask = None
    causal_mask = attentions.causal_mask(inputs)
    attention_mask = jnp.minimum(causal_mask, attention_mask)
    if packed_input:
      segment_ids = np.random.randint(0, 3, [batch_size, seq_len])
      segment_mask = attentions.segment_mask(segment_ids, dtype=np.float32)
      attention_mask = jnp.minimum(attention_mask, segment_mask)
    cross_inputs = None
    cross_paddings = None
    cross_attention_mask = None
    if cross_attention:
      cross_seq_len = np.random.randint(10, 32)
      npy_cross_inputs = np.random.normal(
          1.0, 0.5, [batch_size, cross_seq_len, p.input_dims]).astype('float32')
      cross_inputs = jnp.asarray(npy_cross_inputs)
      npy_cross_paddings = np.random.randint(
          0, 1, [batch_size, cross_seq_len]).astype('float32')
      cross_paddings = jnp.asarray(npy_cross_paddings)
      cross_attention_mask = attentions.convert_paddings_to_mask(cross_paddings)
      if packed_input:
        source_segment_ids = np.random.randint(0, 3,
                                               [batch_size, cross_seq_len])
        cross_segment_mask = attentions.segment_mask(
            segment_ids, source_segment_ids, dtype=np.float32)
        cross_attention_mask = jnp.minimum(cross_attention_mask,
                                           cross_segment_mask)

    with base_layer.JaxContext.new_context():
      _, decoder_state = transformer_layer.apply(
          initial_vars,
          jnp.zeros_like(inputs),
          jnp.ones_like(paddings),
          attention_mask=attention_mask,
          cross_inputs=cross_inputs,
          cross_attention_mask=cross_attention_mask,
          method=transformer_layer.__call__,
          mutable=[DECODE_CACHE])
      fprop_outputs, _ = transformer_layer.apply(
          initial_vars,
          inputs,
          paddings,
          attention_mask=attention_mask,
          cross_inputs=cross_inputs,
          cross_attention_mask=cross_attention_mask,
          method=transformer_layer.__call__)
      decoder_outputs = jnp.zeros(shape=[seq_len, batch_size, p.input_dims])
      updated_vars = py_utils.MergeDictsWithValueCheck(decoder_state,
                                                       initial_vars)
      for t in range(seq_len):
        attention_mask_t = attention_mask[:, :, t, :]
        cross_attention_mask_t = cross_attention_mask
        if cross_attention:
          cross_attention_mask_t = cross_attention_mask[:, :, t, :]
          cross_attention_mask_t = np.expand_dims(
              cross_attention_mask_t, axis=2)
        encoded, decoder_state = transformer_layer.apply(
            updated_vars,
            inputs=inputs[:, t, :],
            time_step=t,
            attention_mask=attention_mask_t,
            cross_inputs=cross_inputs,
            cross_attention_mask=cross_attention_mask_t,
            method=transformer_layer.extend_step,
            mutable=[DECODE_CACHE])
        updated_vars = py_utils.MergeDictsWithValueCheck(
            decoder_state, initial_vars)
        decoder_outputs = decoder_outputs.at[t].set(encoded)

    decoder_out_transposed = jnp.transpose(decoder_outputs, [1, 0, 2])
    logging.info('initial_vars in transformer layer = %s',
                 jax.tree_map(lambda x: x.shape, initial_vars))
    np_fprop_outputs = test_utils.to_np(fprop_outputs)
    np_decoder_outputs = test_utils.to_np(decoder_out_transposed)
    self.assertAllClose(np_fprop_outputs, np_decoder_outputs, atol=1e-5)

  @parameterized.parameters(True, False)
  def test_transformer_layer_cross_attention_ln(self, packed_input):
    input_dims = 8
    hidden_dims = 32
    num_heads = 4
    p = transformers.Transformer.HParams(
        name='jax_transformer_layer',
        input_dims=input_dims,
        hidden_dims=hidden_dims,
        num_heads=num_heads,
        mask_self_attention=True,
        packed_input=packed_input,
        cross_attention=True)
    seq_len = 5
    batch_size = 4
    transformer_layer = instantiate(p)
    prng_key = jax.random.PRNGKey(seed=123)
    initial_vars = transformer_layer.init(prng_key)
    # Change the self attention initial vars.
    initial_vars[PARAMS]['layer_norm']['scale'] = 0.5 * jnp.ones(
        shape=[p.input_dims])
    initial_vars[PARAMS]['layer_norm']['bias'] = 5.0 * jnp.ones(
        shape=[p.input_dims])
    # Change the cross attention initial vars.
    initial_vars[PARAMS]['cross_layer_norm']['scale'] = 15 * jnp.ones(
        shape=[p.input_dims])
    initial_vars[PARAMS]['cross_layer_norm']['bias'] = 1.5 * jnp.ones(
        shape=[p.input_dims])
    npy_inputs = np.random.normal(
        1.0, 0.5, [batch_size, seq_len, p.input_dims]).astype('float32')
    inputs = jnp.asarray(npy_inputs)
    npy_paddings = np.random.randint(0, 1,
                                     [batch_size, seq_len]).astype('float32')
    paddings = jnp.asarray(npy_paddings)
    attention_mask = attentions.convert_paddings_to_mask(paddings)
    causal_mask = attentions.causal_mask(inputs)
    attention_mask = jnp.minimum(causal_mask, attention_mask)
    if packed_input:
      segment_ids = np.random.randint(0, 3, [batch_size, seq_len])
      segment_mask = attentions.segment_mask(segment_ids, dtype=np.float32)
      attention_mask = jnp.minimum(attention_mask, segment_mask)

    layer_norm_p = copy.deepcopy(p.ln_tpl)
    layer_norm_p.set(name='layer_norm', dim=input_dims)
    layer_norm = instantiate(layer_norm_p)
    layer_norm_vars = {PARAMS: initial_vars[PARAMS]['layer_norm']}

    self_attention_p = copy.deepcopy(p.tr_atten_tpl)
    self_attention_p.set(
        name='self_attention',
        input_dim=input_dims,
        hidden_dim=input_dims,
        num_heads=num_heads,
        dim_per_head=None,
        atten_dropout_prob=0.0)
    self_attention = instantiate(self_attention_p)
    prng_key, init_key = jax.random.split(prng_key)
    self_attention_vars = self_attention.init(init_key)

    residual_dropout_p = copy.deepcopy(p.dropout_tpl)
    residual_dropout_p.set(name='residual_dropout', keep_prob=1.)
    residual_dropout = instantiate(residual_dropout_p)
    prng_key, init_key = jax.random.split(prng_key)
    residual_dropout_vars = residual_dropout.init(init_key)

    cross_layer_norm_p = copy.deepcopy(p.ln_tpl)
    cross_layer_norm_p.set(name='cross_layer_norm', dim=input_dims)
    cross_layer_norm = instantiate(cross_layer_norm_p)
    cross_layer_norm_vars = {PARAMS: initial_vars[PARAMS]['cross_layer_norm']}

    with base_layer.JaxContext.new_context():
      inputs_normalized = layer_norm.apply(layer_norm_vars, inputs)
      # Compute self-attention, key/value vectors are the input itself
      atten_output, _ = self_attention.apply(
          self_attention_vars,
          inputs_normalized,
          inputs_normalized,
          inputs_normalized,
          atten_mask=attention_mask)
      # Residual dropout and connection.
      atten_output = residual_dropout.apply(residual_dropout_vars, atten_output)
      atten_output += inputs
      # Normalize atten outputs using cross attention.
      atten_output_normalized = cross_layer_norm.apply(cross_layer_norm_vars,
                                                       atten_output)
    inputs_normalized = test_utils.to_np(inputs_normalized)
    atten_output_normalized = test_utils.to_np(atten_output_normalized)
    self.assertAllClose(
        initial_vars[PARAMS]['layer_norm']['bias'][0],
        inputs_normalized.mean(),
        atol=1e-3)
    self.assertAllClose(
        (1.0 + initial_vars[PARAMS]['layer_norm']['scale'][0])**2,
        np.var(inputs_normalized),
        atol=5e-3)
    self.assertAllClose(
        initial_vars[PARAMS]['cross_layer_norm']['bias'][0],
        atten_output_normalized.mean(),
        atol=1e-3)
    self.assertAllClose(
        (1.0 + initial_vars[PARAMS]['cross_layer_norm']['scale'][0])**2,
        np.var(atten_output_normalized),
        atol=5e-3)

  def test_transformer_layer_cross_attention_dconv_value_error(self):
    p = transformers.Transformer.HParams(
        name='jax_transformer_layer',
        input_dims=8,
        hidden_dims=32,
        num_heads=4,
        cross_attention=True,
        mask_self_attention=True)
    # Enable cross attention.
    p.cross_atten_tpl = copy.deepcopy(p.tr_atten_tpl)
    # Enable depth-wise convolution.
    p.cross_atten_tpl.dconv_qkv = True
    with self.assertRaises(ValueError):
      transformer_layer = instantiate(p)
      prng_key = jax.random.PRNGKey(seed=123)
      _ = transformer_layer.init(prng_key)

  def test_transformer_layer_cross_attention_pos_emb_value_error(self):
    p = transformers.Transformer.HParams(
        name='jax_transformer_layer',
        input_dims=8,
        hidden_dims=32,
        num_heads=4,
        cross_attention=True,
        mask_self_attention=True)
    # Enable cross attention.
    p.cross_atten_tpl = copy.deepcopy(p.tr_atten_tpl)
    # Enable rotary position embedding.
    p.cross_atten_tpl.use_rotary_position_emb = True
    with self.assertRaises(ValueError):
      transformer_layer = instantiate(p)
      prng_key = jax.random.PRNGKey(seed=123)
      _ = transformer_layer.init(prng_key)

  # TODO(lingvo-team): Figure out a solution to use identical Flax prng stream
  # for the two configurations considered. At the moment, the
  # StackedTransformerRepeated and StackedTransformer implementations do not
  # use the same PRNG values in the same order, when computing
  # Top2GatingOnLogits() for the MoE feedforward layer. While the initial
  # weights of the models are identical, this causes the outputs to be
  # significantly different, though correlated.
  @absltest.SkipTest
  @parameterized.parameters('top2', 'expert_choice')
  def test_transformer_moe_dense_layer_gating(self, gating_function):
    # Comparing scan over blocks of layers and regular loop
    block_p = transformers.StackedTransformer.HParams(
        name='transformer_block',
        num_layers=2,
        model_dims=3,
        hidden_dims=6,
        num_heads=1,
        mask_self_attention=True,
        packed_input=True,
        cross_attention=False,
        num_experts=4,
        num_groups=1,
        moe_layers=[0])

    block_p_repeated = transformers.StackedTransformerRepeated.HParams(
        name='stacked_transformer_layer_repeated',
        block=copy.deepcopy(block_p),
        x_times=1)

    stack_p = transformers.StackedTransformer.HParams(
        name='transformer_stack',
        num_layers=2,  # moe + dense
        model_dims=block_p.model_dims,
        hidden_dims=block_p.hidden_dims,
        num_heads=block_p.num_heads,
        mask_self_attention=block_p.mask_self_attention,
        packed_input=block_p.packed_input,
        cross_attention=block_p.cross_attention,
        num_experts=block_p.num_experts,
        num_groups=block_p.num_groups,
        moe_layers=[0])

    moe_p = stack_p.moe_layer_tpl
    moe_p.gating_func = gating_function
    moe_p.num_experts = 4
    moe_p.expert_capacity_dim = 2
    moe_p.expert_capacity_factor = 0

    moe_p = block_p.moe_layer_tpl
    moe_p.gating_func = gating_function
    moe_p.num_experts = 4
    moe_p.expert_capacity_dim = 2
    moe_p.expert_capacity_factor = 0

    transformer_block = instantiate(block_p_repeated)
    transformer_stack = instantiate(stack_p)

    seq_len = 4
    batch_size = 3
    prng_key = jax.random.PRNGKey(seed=123)
    block_initial_vars = transformer_block.init(prng_key)
    stack_initial_vars = transformer_stack.init(prng_key)
    stack_initial_vars[PARAMS]['x_layers_0'] = jax.tree_map(
        lambda x: jnp.squeeze(x, axis=0),
        block_initial_vars[PARAMS]['repeat']['sub']['x_layers_0'])
    stack_initial_vars[PARAMS]['x_layers_1'] = jax.tree_map(
        lambda x: jnp.squeeze(x, axis=0),
        block_initial_vars[PARAMS]['repeat']['sub']['x_layers_1'])
    npy_inputs = np.random.normal(
        1.0, 0.5, [batch_size, seq_len, block_p.model_dims]).astype('float32')
    inputs = jnp.asarray(npy_inputs)
    npy_paddings = np.random.randint(0, 1,
                                     [batch_size, seq_len]).astype('float32')
    paddings = jnp.asarray(npy_paddings)
    segment_mask = None
    segment_ids = np.random.randint(0, 3, [batch_size, seq_len])
    segment_mask = attentions.segment_mask(segment_ids, dtype=np.float32)

    cross_inputs = None
    cross_paddings = None
    cross_segment_mask = None

    with base_layer.JaxContext.new_context():
      prng_key, subkey = jax.random.split(prng_key)
      block_outputs = transformer_block.apply(
          block_initial_vars,
          inputs,
          paddings,
          segment_mask=segment_mask,
          cross_inputs=cross_inputs,
          cross_paddings=cross_paddings,
          cross_segment_mask=cross_segment_mask,
          rngs={RANDOM: subkey})

      stack_outputs = transformer_stack.apply(
          stack_initial_vars,
          inputs,
          paddings,
          segment_mask=segment_mask,
          cross_inputs=cross_inputs,
          cross_paddings=cross_paddings,
          cross_segment_mask=cross_segment_mask,
          rngs={RANDOM: subkey})
    block_np_outputs = test_utils.to_np(block_outputs)
    stack_np_outputs = test_utils.to_np(stack_outputs)
    self.assertAllClose(stack_np_outputs, block_np_outputs, rtol=1e-5)

  @absltest.SkipTest
  @parameterized.parameters(*list(itertools.product([True, False], repeat=3)))
  def test_transformer_moe_dense_layer(self, mask_self_attention, packed_input,
                                       cross_attention):
    # Comparing scan over blocks of layers and regular loop
    block_p = transformers.StackedTransformer.HParams(
        name='transformer_block',
        num_layers=2,
        model_dims=3,
        hidden_dims=6,
        num_heads=1,
        mask_self_attention=mask_self_attention,
        packed_input=packed_input,
        cross_attention=cross_attention,
        num_experts=4,
        num_groups=1,
        moe_layers=[0])

    block_p_repeated = transformers.StackedTransformerRepeated.HParams(
        name='stacked_transformer_layer_repeated',
        block=copy.deepcopy(block_p),
        x_times=1)

    stack_p = transformers.StackedTransformer.HParams(
        name='transformer_stack',
        num_layers=2,  # moe + dense
        model_dims=block_p.model_dims,
        hidden_dims=block_p.hidden_dims,
        num_heads=block_p.num_heads,
        mask_self_attention=block_p.mask_self_attention,
        packed_input=block_p.packed_input,
        cross_attention=block_p.cross_attention,
        num_experts=block_p.num_experts,
        num_groups=block_p.num_groups,
        moe_layers=[0])

    moe_p = stack_p.moe_layer_tpl
    moe_p.expert_capacity_dim = 2
    moe_p.expert_capacity_factor = 0

    moe_p = block_p.moe_layer_tpl
    moe_p.expert_capacity_dim = 2
    moe_p.expert_capacity_factor = 0

    transformer_block = instantiate(block_p_repeated)
    transformer_stack = instantiate(stack_p)

    seq_len = 4
    batch_size = 3
    prng_key = jax.random.PRNGKey(seed=123)
    block_initial_vars = transformer_block.init(prng_key)
    stack_initial_vars = transformer_stack.init(prng_key)
    stack_initial_vars[PARAMS]['x_layers_0'] = jax.tree_map(
        lambda x: jnp.squeeze(x, axis=0),
        block_initial_vars[PARAMS]['repeat']['sub']['x_layers_0'])
    stack_initial_vars[PARAMS]['x_layers_1'] = jax.tree_map(
        lambda x: jnp.squeeze(x, axis=0),
        block_initial_vars[PARAMS]['repeat']['sub']['x_layers_1'])
    npy_inputs = np.random.normal(
        1.0, 0.5, [batch_size, seq_len, block_p.model_dims]).astype('float32')
    inputs = jnp.asarray(npy_inputs)
    npy_paddings = np.random.randint(0, 1,
                                     [batch_size, seq_len]).astype('float32')
    paddings = jnp.asarray(npy_paddings)
    segment_mask = None
    if packed_input:
      segment_ids = np.random.randint(0, 3, [batch_size, seq_len])
      segment_mask = attentions.segment_mask(segment_ids, dtype=np.float32)

    cross_inputs = None
    cross_paddings = None
    cross_segment_mask = None
    if cross_attention:
      cross_seq_len = np.random.randint(10, 64)
      npy_cross_inputs = np.random.normal(
          1.0, 0.5,
          [batch_size, cross_seq_len, block_p.model_dims]).astype('float32')
      cross_inputs = jnp.asarray(npy_cross_inputs)
      npy_cross_paddings = np.random.randint(
          0, 1, [batch_size, cross_seq_len]).astype('float32')
      cross_paddings = jnp.asarray(npy_cross_paddings)
      if packed_input:
        source_segment_ids = np.random.randint(0, 3,
                                               [batch_size, cross_seq_len])
        cross_segment_mask = attentions.segment_mask(
            segment_ids, source_segment_ids, dtype=np.float32)

    with base_layer.JaxContext.new_context():
      prng_key, subkey = jax.random.split(prng_key)
      block_outputs = transformer_block.apply(
          block_initial_vars,
          inputs,
          paddings,
          segment_mask=segment_mask,
          cross_inputs=cross_inputs,
          cross_paddings=cross_paddings,
          cross_segment_mask=cross_segment_mask,
          rngs={RANDOM: subkey})

      stack_outputs = transformer_stack.apply(
          stack_initial_vars,
          inputs,
          paddings,
          segment_mask=segment_mask,
          cross_inputs=cross_inputs,
          cross_paddings=cross_paddings,
          cross_segment_mask=cross_segment_mask,
          rngs={RANDOM: subkey})
    block_np_outputs = test_utils.to_np(block_outputs)
    stack_np_outputs = test_utils.to_np(stack_outputs)
    self.assertAllClose(stack_np_outputs, block_np_outputs, rtol=1e-5)

  @parameterized.parameters(*list(itertools.product([True, False], repeat=3)))
  def test_stacked_transformer_layer(self, mask_self_attention, packed_input,
                                     cross_attention):
    p = transformers.StackedTransformer.HParams(
        name='jax_stacked_transformer_layer',
        model_dims=16,
        hidden_dims=64,
        num_heads=8,
        mask_self_attention=mask_self_attention,
        num_layers=4,
        packed_input=packed_input,
        cross_attention=cross_attention)
    seq_len = np.random.randint(10, 32)
    batch_size = 10
    stacked_transformer_layer = instantiate(p)
    prng_key = jax.random.PRNGKey(seed=123)
    initial_vars = stacked_transformer_layer.init(prng_key)

    npy_inputs = np.random.normal(
        1.0, 0.5, [batch_size, seq_len, p.model_dims]).astype('float32')
    inputs = jnp.asarray(npy_inputs)
    npy_paddings = np.random.randint(0, 1,
                                     [batch_size, seq_len]).astype('float32')
    paddings = jnp.asarray(npy_paddings)
    segment_mask = None
    tf_segment_mask = None
    if packed_input:
      segment_ids = np.random.randint(0, 3, [batch_size, seq_len])
      segment_mask = attentions.segment_mask(segment_ids, dtype=np.float32)
      if mask_self_attention:
        tf_segment_mask = batch_major_attention.CausalSegmentMask(
            segment_ids, tf.float32)
      else:
        tf_segment_mask = batch_major_attention.SegmentMask(
            segment_ids, segment_ids)

    cross_inputs = None
    cross_paddings = None
    cross_segment_mask = None
    tf_cross_inputs = None
    tf_cross_paddings = None
    tf_cross_segment_mask = None
    if cross_attention:
      cross_seq_len = np.random.randint(10, 64)
      npy_cross_inputs = np.random.normal(
          1.0, 0.5, [batch_size, cross_seq_len, p.model_dims]).astype('float32')
      cross_inputs = jnp.asarray(npy_cross_inputs)
      tf_cross_inputs = tf.constant(npy_cross_inputs, dtype=tf.float32)
      npy_cross_paddings = np.random.randint(
          0, 1, [batch_size, cross_seq_len]).astype('float32')
      cross_paddings = jnp.asarray(npy_cross_paddings)
      tf_cross_paddings = tf.constant(npy_cross_paddings, dtype=tf.float32)
      if packed_input:
        source_segment_ids = np.random.randint(0, 3,
                                               [batch_size, cross_seq_len])
        cross_segment_mask = attentions.segment_mask(
            segment_ids, source_segment_ids, dtype=np.float32)
        tf_cross_segment_mask = batch_major_attention.SegmentMask(
            segment_ids, source_segment_ids)

    with base_layer.JaxContext.new_context():
      outputs = stacked_transformer_layer.apply(
          initial_vars,
          inputs,
          paddings,
          segment_mask=segment_mask,
          cross_inputs=cross_inputs,
          cross_paddings=cross_paddings,
          cross_segment_mask=cross_segment_mask)
    logging.info('initial_vars in transformer layer = %s', initial_vars)
    logging.info('initial_vars in stacked_transformer_layer layer = %s',
                 jax.tree_map(lambda x: x.shape, initial_vars))
    # Test whether tf Transformer layer returns same output
    # Modify initial_vars to use TF compatible params
    tf_initial_vars = py_utils.NestedMap()
    tf_initial_vars.x_layers = []
    x_layers_prefix = 'x_layers_'
    x_layers_keys = sorted(
        [k for k in initial_vars[PARAMS] if k.startswith(x_layers_prefix)],
        key=lambda v: int(v[len(x_layers_prefix):]))
    for x_layer_key in x_layers_keys:
      jax_initial_vars = initial_vars[PARAMS][x_layer_key]
      tf_layer_vars = py_utils.NestedMap.FromNestedDict(jax_initial_vars)
      tf_layer_vars = test_utils.replace_jax_attention_vars_to_tf(
          tf_layer_vars, cross_attention)
      tf_initial_vars.x_layers.append(tf_layer_vars)
    tf_initial_vars = test_utils.to_tf_nmap(tf_initial_vars)
    logging.info('tf_initial_vars in transformer layer = %s', initial_vars)
    tf_p = batch_major_attention.StackedTransformerLayers.Params().Set(
        name='tf_transformer_layer',
        mdl_dim=p.model_dims,
        hidden_dim=p.hidden_dims,
        num_atten_heads=p.num_heads,
        mask_self_atten=mask_self_attention,
        num_layers=p.num_layers,
        packed_input=packed_input,
        has_aux_atten=cross_attention)
    tf_p.transformer_layer_params_tpl.tr_fflayer_tpl.fflayer_tpl.batch_norm = (
        False)
    tf_p.transformer_layer_params_tpl.tr_fflayer_tpl.fflayer_tpl.has_bias = True
    tf_stacked_transformer_layer = tf_p.Instantiate()
    if tf_segment_mask is not None:
      tf_segment_mask = test_utils.to_tf_nmap(tf_segment_mask)
    if tf_cross_inputs is not None:
      tf_cross_inputs = test_utils.to_tf_nmap(tf_cross_inputs)
    if tf_cross_paddings is not None:
      tf_cross_paddings = test_utils.to_tf_nmap(tf_cross_paddings)
    if tf_cross_segment_mask is not None:
      tf_cross_segment_mask = test_utils.to_tf_nmap(tf_cross_segment_mask)
    tf_output, _ = tf_stacked_transformer_layer.FProp(
        tf_initial_vars,
        test_utils.to_tf_nmap(npy_inputs),
        paddings=test_utils.to_tf_nmap(npy_paddings),
        segment_mask=tf_segment_mask,
        aux_vec=tf_cross_inputs,
        aux_paddings=tf_cross_paddings,
        aux_segment_mask=tf_cross_segment_mask)
    np_outputs = test_utils.to_np(outputs)
    tf_np_outputs = test_utils.to_np(tf_output)
    self.assertAllClose(tf_np_outputs, np_outputs, atol=1e-5)

  @parameterized.parameters(*list(itertools.product([True, False], repeat=3)))
  def test_repeated_stacked_xformer_layer(self, mask_self_attention,
                                          packed_input, cross_attention):
    model_dims = 16
    num_layers = 4
    p1 = transformers.StackedTransformer.HParams(
        name='jax_stacked_transformer_layer',
        model_dims=model_dims,
        hidden_dims=64,
        num_heads=8,
        mask_self_attention=mask_self_attention,
        num_layers=num_layers,
        packed_input=packed_input,
        cross_attention=cross_attention)
    p1_one_layer = copy.deepcopy(p1)
    p1_one_layer.num_layers = 1
    p2 = transformers.StackedTransformerRepeated.HParams(
        name='jax_stacked_transformer_layer_repeated',
        block=p1_one_layer,
        x_times=p1.num_layers)
    seq_len = np.random.randint(10, 32)
    batch_size = 10
    stacked_transformer_layer = instantiate(p1)
    repeated_transformer_layer = instantiate(p2)
    prng_key = jax.random.PRNGKey(seed=123)

    initial_vars = stacked_transformer_layer.init(prng_key)

    def _stack_vars(*args):
      args = [x[jnp.newaxis, :] for x in args]
      return jnp.vstack(args)

    x_layers = []
    for i in range(num_layers):
      x_layers.append(initial_vars[PARAMS]['x_layers_' + str(i)])
    stacked_x_layers = tf.nest.map_structure(_stack_vars, *x_layers)
    repeated_vars = repeated_transformer_layer.init(prng_key)
    repeated_vars = py_utils.NestedMap(
        params=py_utils.NestedMap(
            repeat=py_utils.NestedMap(
                sub=py_utils.NestedMap(x_layers_0=stacked_x_layers))))

    tf.nest.assert_same_structure(repeated_vars,
                                  repeated_transformer_layer.init(prng_key))

    npy_inputs = np.random.normal(
        1.0, 0.5, [batch_size, seq_len, model_dims]).astype('float32')
    inputs = jnp.asarray(npy_inputs)
    npy_paddings = np.random.randint(0, 1,
                                     [batch_size, seq_len]).astype('float32')
    paddings = jnp.asarray(npy_paddings)
    segment_mask = None
    if packed_input:
      segment_ids = np.random.randint(0, 3, [batch_size, seq_len])
      segment_mask = attentions.segment_mask(segment_ids, dtype=np.float32)

    cross_inputs = None
    cross_paddings = None
    cross_segment_mask = None
    if cross_attention:
      cross_seq_len = np.random.randint(10, 64)
      npy_cross_inputs = np.random.normal(
          1.0, 0.5, [batch_size, cross_seq_len, model_dims]).astype('float32')
      cross_inputs = jnp.asarray(npy_cross_inputs)
      npy_cross_paddings = np.random.randint(
          0, 1, [batch_size, cross_seq_len]).astype('float32')
      cross_paddings = jnp.asarray(npy_cross_paddings)
      if packed_input:
        source_segment_ids = np.random.randint(0, 3,
                                               [batch_size, cross_seq_len])
        cross_segment_mask = attentions.segment_mask(
            segment_ids, source_segment_ids, dtype=np.float32)

    with base_layer.JaxContext.new_context():
      outputs = stacked_transformer_layer.apply(
          initial_vars,
          inputs,
          paddings,
          segment_mask=segment_mask,
          cross_inputs=cross_inputs,
          cross_paddings=cross_paddings,
          cross_segment_mask=cross_segment_mask)

      outputs_repeated = repeated_transformer_layer.apply(
          repeated_vars,
          inputs,
          paddings,
          segment_mask=segment_mask,
          cross_inputs=cross_inputs,
          cross_paddings=cross_paddings,
          cross_segment_mask=cross_segment_mask)
    self.assertAllClose(outputs, outputs_repeated, atol=1e-5)

  @parameterized.parameters(*list(itertools.product([True, False], repeat=5)))
  def test_stacked_transformer_layer_extendstep(self, packed_input,
                                                cross_attention, combine_qkv,
                                                dconv_qkv,
                                                use_rotary_position_emb):
    if cross_attention and combine_qkv:
      self.skipTest('combine_qkv optimization only works for self-attention.')
    layer_params = transformers.StackedTransformer.HParams()

    num_layers = 2
    model_dims = 8
    p = layer_params.set(
        name='jax_transformer_layer',
        model_dims=model_dims,
        hidden_dims=32,
        num_heads=2,
        mask_self_attention=True,
        packed_input=packed_input,
        cross_attention=cross_attention,
        num_layers=num_layers)
    p.transformer_layer_params_tpl.tr_atten_tpl.combine_qkv = combine_qkv
    p.transformer_layer_params_tpl.tr_atten_tpl.dconv_qkv = dconv_qkv
    p.transformer_layer_params_tpl.tr_atten_tpl.use_rotary_position_emb = (
        use_rotary_position_emb)
    if cross_attention:
      p.transformer_layer_params_tpl.cross_atten_tpl = copy.deepcopy(
          p.transformer_layer_params_tpl.tr_atten_tpl)
      # Cross attention should not have depth-wise convolution.
      p.transformer_layer_params_tpl.cross_atten_tpl.dconv_qkv = False
      # Cross attention should not have rotary position embedding.
      p.transformer_layer_params_tpl.cross_atten_tpl.use_rotary_position_emb = (
          False)

    p_copy = copy.deepcopy(p)
    p_copy.num_layers = 1
    p = transformers.StackedTransformerRepeated.HParams()
    p.name = 'jax_transformer_repeated_layer'
    p.block = p_copy
    p.x_times = num_layers

    seq_len = 4
    batch_size = 4
    repeat_transformer_layer = instantiate(p)
    prng_key = jax.random.PRNGKey(seed=123)
    initial_vars = repeat_transformer_layer.init(prng_key)
    npy_inputs = np.random.normal(
        1.0, 0.5, [batch_size, seq_len, model_dims]).astype('float32')
    inputs = jnp.asarray(npy_inputs)
    npy_paddings = np.random.randint(0, 1,
                                     [batch_size, seq_len]).astype('float32')
    paddings = jnp.asarray(npy_paddings)
    segment_mask = None
    segment_pos = None
    if packed_input:
      segment_ids = np.maximum(
          np.random.randint(0, 2, [batch_size, seq_len]),
          npy_paddings.astype('int32'))
      segment_ids = np.cumsum(segment_ids, axis=1)
      segment_pos = np.zeros_like(segment_ids)
      for b in range(batch_size):
        for t in range(1, seq_len):
          if (segment_ids[b, t] == segment_ids[b, t - 1]):
            segment_pos[b, t] = segment_pos[b, t - 1] + 1
      segment_mask = attentions.segment_mask(segment_ids, dtype=np.float32)
      segment_pos = jnp.asarray(segment_pos)

    cross_inputs = None
    cross_paddings = None
    cross_segment_mask = None
    if cross_attention:
      cross_seq_len = np.random.randint(10, 32)
      npy_cross_inputs = np.random.normal(
          1.0, 0.5, [batch_size, cross_seq_len, model_dims]).astype('float32')
      cross_inputs = jnp.asarray(npy_cross_inputs)
      npy_cross_paddings = np.random.randint(
          0, 1, [batch_size, cross_seq_len]).astype('float32')
      cross_paddings = jnp.asarray(npy_cross_paddings)
      if packed_input:
        source_segment_ids = np.random.randint(0, 3,
                                               [batch_size, cross_seq_len])
        cross_segment_mask = attentions.segment_mask(
            segment_ids, source_segment_ids, dtype=np.float32)

    prng_key = jax.random.PRNGKey(seed=123)
    with base_layer.JaxContext.new_context():
      fprop_outputs = repeat_transformer_layer.apply(
          initial_vars,
          inputs,
          paddings,
          segment_pos=segment_pos,
          segment_mask=segment_mask,
          cross_inputs=cross_inputs,
          cross_paddings=cross_paddings,
          cross_segment_mask=cross_segment_mask)
      _, decoder_state = repeat_transformer_layer.apply(
          initial_vars,
          jnp.zeros_like(inputs),
          jnp.ones_like(paddings),
          segment_mask=segment_mask,
          cross_inputs=cross_inputs,
          cross_paddings=cross_paddings,
          cross_segment_mask=cross_segment_mask,
          mutable=[DECODE_CACHE])

      decoder_outputs = jnp.zeros(shape=[seq_len, batch_size, model_dims])
      updated_vars = py_utils.MergeDictsWithValueCheck(decoder_state,
                                                       initial_vars)
      for t in range(seq_len):
        cross_segment_mask_t = cross_segment_mask
        segment_pos_t = None
        if segment_mask is not None:
          segment_pos_t = segment_pos[:, t]
        if cross_segment_mask is not None:
          cross_segment_mask_t = cross_segment_mask[:, :, t, :]
        encoded, decoder_state = repeat_transformer_layer.apply(
            updated_vars,
            inputs=inputs[:, t, :],
            time_step=t,
            segment_pos=segment_pos_t,
            cross_inputs=cross_inputs,
            cross_paddings=cross_paddings,
            cross_segment_mask=cross_segment_mask_t,
            method=repeat_transformer_layer.extend_step,
            mutable=[DECODE_CACHE])
        updated_vars = py_utils.MergeDictsWithValueCheck(
            decoder_state, initial_vars)
        encoded, _ = encoded
        decoder_outputs = decoder_outputs.at[t].set(encoded)

    decoder_out_transposed = jnp.transpose(decoder_outputs, [1, 0, 2])
    # Compare only the non-padding tokens since the padding mask is not applied
    # to the padding token itself in decoding.
    non_pad = (1 - paddings)[:, :, np.newaxis]
    # TODO(lepikhin): remove noisy test logging
    np_fprop_outputs = test_utils.to_np(fprop_outputs) * non_pad
    np_decoder_outputs = test_utils.to_np(decoder_out_transposed) * non_pad
    self.assertAllClose(np_fprop_outputs, np_decoder_outputs, atol=1e-5)

  @parameterized.parameters('RELU', 'SILU', 'GATED_SILU')
  def test_transformer_feedforward(self, activation_function):
    p = transformers.TransformerFeedForward.HParams(
        name='ffwd',
        input_dims=8,
        hidden_dims=32,
        activation=activation_function)
    batch_size = 8
    seq_len = 512
    ffwd = instantiate(p)
    prng_key = jax.random.PRNGKey(seed=123)
    initial_vars = ffwd.init(prng_key)

    npy_inputs = np.random.normal(
        1.0, 0.5, [batch_size, seq_len, p.input_dims]).astype('float32')
    inputs = jnp.asarray(npy_inputs)
    npy_paddings = np.zeros([batch_size, seq_len], dtype=np.float32)
    input_paddings = jnp.asarray(npy_paddings)

    with base_layer.JaxContext.new_context():
      outputs = ffwd.apply(initial_vars, inputs, input_paddings)
      logging.info('outputs: %s', outputs)

    if activation_function.startswith('GATED_'):
      # Default lingvo layers_with_attention.TransformerFeedForwardLayer does
      # not support gating.
      return

    # Test whether Tensorflow TransformerFeedForwardLayer returns the same
    # output. Modify `initial_vars` to use TF compatible params.
    tf_initial_vars = py_utils.NestedMap.FromNestedDict(initial_vars[PARAMS])
    tf_initial_vars = test_utils.replace_jax_transformer_ffwd_vars_to_tf(
        tf_initial_vars)
    tf_initial_vars = test_utils.to_tf_nmap(tf_initial_vars)
    logging.info('tf_initial_vars in transformer feedforward layer = %s',
                 initial_vars)
    tf_p = layers_with_attention.TransformerFeedForwardLayer.Params().Set(
        name='tf_ffwd',
        input_dim=p.input_dims,
        hidden_dim=p.hidden_dims,
        activation=p.activation)
    tf_ffwd = tf_p.Instantiate()
    tf_output = tf_ffwd.FProp(
        tf_initial_vars,
        tf.constant(npy_inputs, dtype=tf.float32),
        paddings=test_utils.to_tf_nmap(npy_paddings))
    np_outputs = test_utils.to_np(outputs)
    tf_np_outputs = test_utils.to_np(tf_output)
    self.assertAllClose(tf_np_outputs, np_outputs, atol=1e-5)

  @parameterized.parameters(['pre', 'primer_hybrid', 'post'])
  def test_transformer_layer_norm_policies(self, norm_policy):
    p = transformers.Transformer.HParams(
        name='jax_transformer_layer',
        input_dims=32,
        hidden_dims=128,
        num_heads=8,
        mask_self_attention=True,
        packed_input=True,
        cross_attention=False,
        norm_policy=norm_policy)
    seq_len = np.random.randint(10, 32)
    batch_size = 10
    transformer_layer = instantiate(p)
    prng_key = jax.random.PRNGKey(seed=123)
    initial_vars = transformer_layer.init(prng_key)
    npy_inputs = np.random.normal(
        1.0, 0.5, [batch_size, seq_len, p.input_dims]).astype('float32')
    inputs = jnp.asarray(npy_inputs)
    npy_paddings = np.random.randint(0, 1,
                                     [batch_size, seq_len]).astype('float32')
    paddings = jnp.asarray(npy_paddings)
    attention_mask = attentions.convert_paddings_to_mask(paddings)
    causal_mask = attentions.causal_mask(inputs)
    attention_mask = jnp.minimum(attention_mask, causal_mask)
    segment_ids = np.random.randint(0, 3, [batch_size, seq_len])
    segment_mask = attentions.segment_mask(segment_ids, dtype=np.float32)
    attention_mask = jnp.minimum(attention_mask, segment_mask)

    with base_layer.JaxContext.new_context():
      outputs, _ = transformer_layer.apply(
          initial_vars, inputs, paddings, attention_mask=attention_mask)
    logging.info('initial_vars in transformer layer = %s',
                 jax.tree_map(lambda x: x.shape, initial_vars))

    np_outputs = test_utils.to_np(outputs)
    # Plumbing test.
    self.assertAllClose(np_outputs, np_outputs, atol=1e-5)

  @parameterized.parameters(['pre', 'primer_hybrid', 'post'])
  def test_transformer_cross_attention_layer_norm_policies(self, norm_policy):
    p = transformers.Transformer.HParams(
        name='jax_transformer_layer',
        input_dims=32,
        hidden_dims=128,
        num_heads=8,
        mask_self_attention=True,
        packed_input=True,
        cross_attention=True,
        norm_policy=norm_policy)
    seq_len = np.random.randint(10, 32)
    batch_size = 10
    transformer_layer = instantiate(p)
    prng_key = jax.random.PRNGKey(seed=123)
    initial_vars = transformer_layer.init(prng_key)
    npy_inputs = np.random.normal(
        1.0, 0.5, [batch_size, seq_len, p.input_dims]).astype('float32')
    inputs = jnp.asarray(npy_inputs)
    npy_paddings = np.random.randint(0, 1,
                                     [batch_size, seq_len]).astype('float32')
    paddings = jnp.asarray(npy_paddings)
    attention_mask = attentions.convert_paddings_to_mask(paddings)
    causal_mask = attentions.causal_mask(inputs)
    attention_mask = jnp.minimum(attention_mask, causal_mask)
    segment_ids = np.random.randint(0, 3, [batch_size, seq_len])
    segment_mask = attentions.segment_mask(segment_ids, dtype=np.float32)
    attention_mask = jnp.minimum(attention_mask, segment_mask)

    with base_layer.JaxContext.new_context():
      outputs, _ = transformer_layer.apply(
          initial_vars, inputs, paddings, attention_mask=attention_mask,
          cross_inputs=inputs, cross_attention_mask=attention_mask)
    logging.info('initial_vars in transformer layer = %s',
                 jax.tree_map(lambda x: x.shape, initial_vars))

    np_outputs = test_utils.to_np(outputs)
    # Plumbing test.
    self.assertAllClose(np_outputs, np_outputs, atol=1e-5)

  @parameterized.parameters([True, False])
  def test_transformer_relative_bias(self, use_relative_bias):
    p = transformers.Transformer.HParams(
        name='jax_transformer_layer',
        input_dims=32,
        hidden_dims=128,
        num_heads=8,
        mask_self_attention=True,
        packed_input=True,
        cross_attention=False)
    seq_len = np.random.randint(10, 32)
    batch_size = 10
    if use_relative_bias:
      p.tr_atten_tpl.relative_bias_tpl = attentions.RelativeBias.HParams(
          relative_attention_num_buckets=2, relative_attention_max_distance=8)
    transformer_layer = instantiate(p)
    prng_key = jax.random.PRNGKey(seed=123)
    initial_vars = transformer_layer.init(prng_key)
    npy_inputs = np.random.normal(
        1.0, 0.5, [batch_size, seq_len, p.input_dims]).astype('float32')
    inputs = jnp.asarray(npy_inputs)
    npy_paddings = np.random.randint(0, 1,
                                     [batch_size, seq_len]).astype('float32')
    paddings = jnp.asarray(npy_paddings)
    attention_mask = attentions.convert_paddings_to_mask(paddings)
    causal_mask = attentions.causal_mask(inputs)
    attention_mask = jnp.minimum(attention_mask, causal_mask)
    segment_ids = np.random.randint(0, 3, [batch_size, seq_len])
    segment_mask = attentions.segment_mask(segment_ids, dtype=np.float32)
    attention_mask = jnp.minimum(attention_mask, segment_mask)

    if use_relative_bias:
      segment_pos = np.random.randint(0, seq_len,
                                      [batch_size, seq_len]).astype('int32')
      segment_pos = jnp.asarray(segment_pos)
    else:
      segment_pos = None

    with base_layer.JaxContext.new_context():
      outputs, _ = transformer_layer.apply(
          initial_vars,
          inputs,
          paddings,
          attention_mask=attention_mask,
          segment_pos=segment_pos)
    logging.info('initial_vars in transformer layer = %s',
                 jax.tree_map(lambda x: x.shape, initial_vars))

    np_outputs = test_utils.to_np(outputs)
    logging.info('np_outputs: %s', np_outputs)
    if use_relative_bias:
      self.assertAlmostEqual(np_outputs[0, 0, 1], -0.033656955, places=5)
      self.assertAlmostEqual(np_outputs[0, 1, 0], 0.3590616, places=5)
    # Plumbing test.
    self.assertAllClose(np_outputs, np_outputs, atol=1e-5)


class RetroTransformersTest(test_utils.TestCase):
  """Test cases for RETRO Transformer."""

  def setUp(self):
    super().setUp()
    np.random.seed(123456)
    tf.random.set_seed(123)

  @dataclasses.dataclass
  class NeighborSize:
    batch: int
    num_chunks: int
    num_neighbors: int
    chunk_length: int
    dim: int

  def prepare_neighbors(self, neighbor_size):
    return np.random.randint(0, 100, size=dataclasses.astuple(neighbor_size))

  def test_retro_transformer_layer(self):
    p = transformers.RetroTransformer.HParams(
        name='jax_retro_transformer_layer',
        input_dims=32,
        hidden_dims=128,
        num_heads=8,
        mask_self_attention=False,
        # TODO(yuancao): Test packed input cases when supported.
        packed_input=False)
    seq_len = 12
    batch_size = 4
    transformer_layer = instantiate(p)
    prng_key = jax.random.PRNGKey(seed=123)
    initial_vars = transformer_layer.init(prng_key)
    npy_inputs = np.random.normal(
        1.0, 0.5, [batch_size, seq_len, p.input_dims]).astype('float32')
    inputs = jnp.asarray(npy_inputs)
    npy_paddings = np.random.randint(0, 1,
                                     [batch_size, seq_len]).astype('float32')
    paddings = jnp.asarray(npy_paddings)
    attention_mask = attentions.convert_paddings_to_mask(paddings)
    neighbors = self.prepare_neighbors(
        self.NeighborSize(
            batch=batch_size,
            num_chunks=3,
            num_neighbors=2,
            chunk_length=5,
            dim=p.input_dims))

    with base_layer.JaxContext.new_context():
      outputs, unused_atten_probs = transformer_layer.apply(
          initial_vars,
          inputs,
          paddings,
          attention_mask=attention_mask,
          neighbors=neighbors)
      self.assertEqual(outputs.shape, (batch_size, seq_len, p.input_dims))

  def test_stacked_retro_layer(self):
    p = transformers.StackedRetroTransformer.HParams(
        name='jax_stacked_retro_transformer_layer',
        transformer_layer_params_tpl=transformers.RetroTransformer.HParams(),
        model_dims=16,
        hidden_dims=64,
        num_heads=8,
        mask_self_attention=False,
        num_layers=4,
        # TODO(yuancao): Test packed input cases when supported.
        packed_input=False)
    seq_len = 12
    batch_size = 4
    stacked_transformer_layer = instantiate(p)
    prng_key = jax.random.PRNGKey(seed=123)
    initial_vars = stacked_transformer_layer.init(prng_key)

    npy_inputs = np.random.normal(
        1.0, 0.5, [batch_size, seq_len, p.model_dims]).astype('float32')
    inputs = jnp.asarray(npy_inputs)
    npy_paddings = np.random.randint(0, 1,
                                     [batch_size, seq_len]).astype('float32')
    paddings = jnp.asarray(npy_paddings)
    segment_mask = None

    neighbors = self.prepare_neighbors(
        self.NeighborSize(
            batch=batch_size,
            num_chunks=3,
            num_neighbors=2,
            chunk_length=5,
            dim=p.model_dims))

    with base_layer.JaxContext.new_context():
      outputs = stacked_transformer_layer.apply(
          initial_vars,
          inputs,
          paddings,
          segment_mask=segment_mask,
          neighbors=neighbors)
    np_outputs = test_utils.to_np(outputs)
    # self.assertAllClose(tf_np_outputs, np_outputs, atol=1e-5)
    print(np_outputs.shape)


if __name__ == '__main__':
  absltest.main()
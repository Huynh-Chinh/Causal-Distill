# coding=utf-8
# Copyright 2019-present, the HuggingFace Inc. team, The Google AI Language Team and Facebook, Inc.
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
"""
 PyTorch DistilRoBERTa model adapted in part from Facebook, Inc XLM model (https://github.com/facebookresearch/XLM) and in
 part from HuggingFace PyTorch version of Google AI Bert model (https://github.com/google-research/bert)
"""

import math

import numpy as np
import torch
from packaging import version
from torch import nn

from transformers.activations import gelu
from transformers.deepspeed import is_deepspeed_zero3_enabled
from transformers.file_utils import (
    add_code_sample_docstrings,
    add_start_docstrings,
    add_start_docstrings_to_model_forward,
    replace_return_docstrings,
)
from transformers.modeling_outputs import (
    BaseModelOutput,
    MaskedLMOutput
)
from transformers.modeling_utils import (
    PreTrainedModel,
    apply_chunking_to_forward,
    find_pruneable_heads_and_indices,
    prune_linear_layer,
)
from transformers.utils import logging
from transformers import (
    RobertaConfig,
)

logger = logging.get_logger(__name__)
# _CHECKPOINT_FOR_DOC = "roberta-base-cased"
# _CONFIG_FOR_DOC = "RobertaConfig"
# _TOKENIZER_FOR_DOC = "RobertaTokenizer"

DISTILROBERTA_PRETRAINED_MODEL_ARCHIVE_LIST = [
    "roberta-base-cased",
    "roberta-large-cased",
]


# UTILS AND BUILDING BLOCKS OF THE ARCHITECTURE #

def create_sinusoidal_embeddings(n_pos, dim, out):
    if is_deepspeed_zero3_enabled():
        import deepspeed

        with deepspeed.zero.GatheredParameters(out, modifier_rank=0):
            if torch.distributed.get_rank() == 0:
                _create_sinusoidal_embeddings(n_pos=n_pos, dim=dim, out=out)
    else:
        _create_sinusoidal_embeddings(n_pos=n_pos, dim=dim, out=out)


def _create_sinusoidal_embeddings(n_pos, dim, out):
    position_enc = np.array([[pos / np.power(10000, 2 * (j // 2) / dim) for j in range(dim)] for pos in range(n_pos)])
    out.requires_grad = False
    out[:, 0::2] = torch.FloatTensor(np.sin(position_enc[:, 0::2]))
    out[:, 1::2] = torch.FloatTensor(np.cos(position_enc[:, 1::2]))
    out.detach_()


class Embeddings(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.word_embeddings = nn.Embedding(config.vocab_size, config.hidden_size, padding_idx=config.pad_token_id)
        self.position_embeddings = nn.Embedding(config.max_position_embeddings, config.hidden_size)
        if config.sinusoidal_pos_embds:
            create_sinusoidal_embeddings(
                n_pos=config.max_position_embeddings, dim=config.hidden_size, out=self.position_embeddings.weight
            )

        self.LayerNorm = nn.LayerNorm(config.hidden_size, eps=1e-12)
        self.dropout = nn.Dropout(config.dropout)
        if version.parse(torch.__version__) > version.parse("1.6.0"):
            self.register_buffer(
                "position_ids", torch.arange(config.max_position_embeddings).expand((1, -1)), persistent=False
            )

    def forward(self, input_ids):
        """
        Parameters:
            input_ids: torch.tensor(bs, max_seq_length) The token ids to embed.
        Returns: torch.tensor(bs, max_seq_length, dim) The embedded tokens (plus position embeddings, no token_type
        embeddings)
        """
        seq_length = input_ids.size(1)

        # Setting the position-ids to the registered buffer in constructor, it helps
        # when tracing the model without passing position-ids, solves
        # isues similar to issue #5664
        if hasattr(self, "position_ids"):
            position_ids = self.position_ids[:, :seq_length]
        else:
            position_ids = torch.arange(seq_length, dtype=torch.long, device=input_ids.device)  # (max_seq_length)
            position_ids = position_ids.unsqueeze(0).expand_as(input_ids)  # (bs, max_seq_length)

        word_embeddings = self.word_embeddings(input_ids)  # (bs, max_seq_length, dim)
        position_embeddings = self.position_embeddings(position_ids)  # (bs, max_seq_length, dim)

        embeddings = word_embeddings + position_embeddings  # (bs, max_seq_length, dim)
        embeddings = self.LayerNorm(embeddings)  # (bs, max_seq_length, dim)
        embeddings = self.dropout(embeddings)  # (bs, max_seq_length, dim)
        return embeddings


class MultiHeadSelfAttention(nn.Module):
    def __init__(self, config):
        super().__init__()

        self.num_attention_heads = config.num_attention_heads
        self.dim = config.hidden_size
        self.dropout = nn.Dropout(p=config.attention_dropout)

        assert self.dim % self.num_attention_heads == 0

        self.q_lin = nn.Linear(in_features=config.hidden_size, out_features=config.hidden_size)
        self.k_lin = nn.Linear(in_features=config.hidden_size, out_features=config.hidden_size)
        self.v_lin = nn.Linear(in_features=config.hidden_size, out_features=config.hidden_size)
        self.out_lin = nn.Linear(in_features=config.hidden_size, out_features=config.hidden_size)

        self.pruned_heads = set()

    def prune_heads(self, heads):
        attention_head_size = self.dim // self.num_attention_heads
        if len(heads) == 0:
            return
        heads, index = find_pruneable_heads_and_indices(heads, self.num_attention_heads, attention_head_size,
                                                        self.pruned_heads)
        # Prune linear layers
        self.q_lin = prune_linear_layer(self.q_lin, index)
        self.k_lin = prune_linear_layer(self.k_lin, index)
        self.v_lin = prune_linear_layer(self.v_lin, index)
        self.out_lin = prune_linear_layer(self.out_lin, index, dim=1)
        # Update hyper params
        self.num_attention_heads = self.num_attention_heads - len(heads)
        self.dim = attention_head_size * self.num_attention_heads
        self.pruned_heads = self.pruned_heads.union(heads)

    def forward(self, query, key, value, mask, head_mask=None, output_attentions=False):
        """
        Parameters:
            query: torch.tensor(bs, seq_length, dim)
            key: torch.tensor(bs, seq_length, dim)
            value: torch.tensor(bs, seq_length, dim)
            mask: torch.tensor(bs, seq_length)
        Returns:
            weights: torch.tensor(bs, n_heads, seq_length, seq_length) Attention weights context: torch.tensor(bs,
            seq_length, dim) Contextualized layer. Optional: only if `output_attentions=True`
        """
        bs, q_length, dim = query.size()
        k_length = key.size(1)
        # assert dim == self.dim, f'Dimensions do not match: {dim} input vs {self.dim} configured'
        # assert key.size() == value.size()

        dim_per_head = self.dim // self.num_attention_heads

        mask_reshp = (bs, 1, 1, k_length)

        def shape(x):
            """separate heads"""
            return x.view(bs, -1, self.num_attention_heads, dim_per_head).transpose(1, 2)

        def unshape(x):
            """group heads"""
            return x.transpose(1, 2).contiguous().view(bs, -1, self.num_attention_heads * dim_per_head)

        q = shape(self.q_lin(query))  # (bs, n_heads, q_length, dim_per_head)
        k = shape(self.k_lin(key))  # (bs, n_heads, k_length, dim_per_head)
        v = shape(self.v_lin(value))  # (bs, n_heads, k_length, dim_per_head)

        q = q / math.sqrt(dim_per_head)  # (bs, n_heads, q_length, dim_per_head)
        scores = torch.matmul(q, k.transpose(2, 3))  # (bs, n_heads, q_length, k_length)
        mask = (mask == 0).view(mask_reshp).expand_as(scores)  # (bs, n_heads, q_length, k_length)
        scores.masked_fill_(mask, -float("inf"))  # (bs, n_heads, q_length, k_length)

        weights = nn.Softmax(dim=-1)(scores)  # (bs, n_heads, q_length, k_length)
        weights = self.dropout(weights)  # (bs, n_heads, q_length, k_length)

        # Mask heads if we want to
        if head_mask is not None:
            weights = weights * head_mask

        context = torch.matmul(weights, v)  # (bs, n_heads, q_length, dim_per_head)
        context = unshape(context)  # (bs, q_length, dim)
        context = self.out_lin(context)  # (bs, q_length, dim)

        if output_attentions:
            return (context, weights)
        else:
            return (context,)


class FFN(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dropout = nn.Dropout(p=config.dropout)
        self.chunk_size_feed_forward = config.chunk_size_feed_forward
        self.seq_len_dim = 1
        self.lin1 = nn.Linear(in_features=config.hidden_size, out_features=config.hidden_dim)
        self.lin2 = nn.Linear(in_features=config.hidden_dim, out_features=config.hidden_size)
        assert config.activation in ["relu", "gelu"], f"activation ({config.activation}) must be in ['relu', 'gelu']"
        self.activation = gelu if config.activation == "gelu" else nn.ReLU()

    def forward(self, input):
        return apply_chunking_to_forward(self.ff_chunk, self.chunk_size_feed_forward, self.seq_len_dim, input)

    def ff_chunk(self, input):
        x = self.lin1(input)
        x = self.activation(x)
        x = self.lin2(x)
        x = self.dropout(x)
        return x


class TransformerBlock(nn.Module):
    def __init__(self, config):
        super().__init__()

        assert config.hidden_size % config.num_attention_heads == 0

        self.attention = MultiHeadSelfAttention(config)
        self.sa_layer_norm = nn.LayerNorm(normalized_shape=config.hidden_size, eps=1e-12)

        self.ffn = FFN(config)
        self.output_layer_norm = nn.LayerNorm(normalized_shape=config.hidden_size, eps=1e-12)

    def forward(self, x, attn_mask=None, head_mask=None, output_attentions=False):
        """
        Parameters:
            x: torch.tensor(bs, seq_length, dim)
            attn_mask: torch.tensor(bs, seq_length)
        Returns:
            sa_weights: torch.tensor(bs, n_heads, seq_length, seq_length) The attention weights ffn_output:
            torch.tensor(bs, seq_length, dim) The output of the transformer block contextualization.
        """
        # Self-Attention
        sa_output = self.attention.forward(
            query=x,
            key=x,
            value=x,
            mask=attn_mask,
            head_mask=head_mask,
            output_attentions=output_attentions,
        )
        if output_attentions:
            sa_output, sa_weights = sa_output  # (bs, seq_length, dim), (bs, n_heads, seq_length, seq_length)
        else:  # To handle these `output_attentions` or `output_hidden_states` cases returning tuples
            assert type(sa_output) == tuple
            sa_output = sa_output[0]
        sa_output = self.sa_layer_norm(sa_output + x)  # (bs, seq_length, dim)

        # Feed Forward Network
        ffn_output = self.ffn.forward(sa_output)  # (bs, seq_length, dim)
        ffn_output = self.output_layer_norm(ffn_output + sa_output)  # (bs, seq_length, dim)

        output = (ffn_output,)
        if output_attentions:
            output = (sa_weights,) + output
        return output


class Transformer(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.num_hidden_layers = config.num_hidden_layers
        self.layer = nn.ModuleList([TransformerBlock(config) for _ in range(config.num_hidden_layers)])
        self.config = config
        self.head_dimension = config.hidden_size // config.num_attention_heads

    def forward(
            self, x, attn_mask=None,
            head_mask=None,
            output_attentions=False,
            output_hidden_states=False,
            return_dict=None,
            # for interchange.
            interchanged_variables=None,
            variable_names=None,
            interchange_mask=None,
            dual_interchange_mask=None,
    ):  # docstyle-ignore
        """
        Parameters:
            x: torch.tensor(bs, seq_length, dim) Input sequence embedded.
            attn_mask: torch.tensor(bs, seq_length) Attention mask on the sequence.
        Returns:
            hidden_state: torch.tensor(bs, seq_length, dim) Sequence of hidden states in the last (top)
            layer all_hidden_states: Tuple[torch.tensor(bs, seq_length, dim)]
                Tuple of length n_layers with the hidden states from each layer.
                Optional: only if output_hidden_states=True
            all_attentions: Tuple[torch.tensor(bs, n_heads, seq_length, seq_length)]
                Tuple of length n_layers with the attention weights from each layer
                Optional: only if output_attentions=True
        """
        all_hidden_states = () if output_hidden_states else None
        all_attentions = () if output_attentions else None

        hidden_state = x
        for i, layer_module in enumerate(self.layer):
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_state,)

            layer_outputs = layer_module(
                x=hidden_state, attn_mask=attn_mask, head_mask=head_mask[i], output_attentions=output_attentions
            )

            hidden_state = layer_outputs[-1]

            # we need to interchange!
            if variable_names != None and variable_names != "embeddings" and i in variable_names:
                assert interchanged_variables != None
                for interchanged_variable in variable_names[i]:
                    interchanged_activations = interchanged_variables[interchanged_variable[0]]
                    start_index = interchanged_variable[1] * self.head_dimension + interchanged_variable[2].start
                    stop_index = start_index + interchanged_variable[2].stop
                    replacing_activations = interchanged_activations[dual_interchange_mask]
                    hidden_state[..., start_index:stop_index][interchange_mask] = replacing_activations

            if output_attentions:
                assert len(layer_outputs) == 2
                attentions = layer_outputs[0]
                all_attentions = all_attentions + (attentions,)
            else:
                assert len(layer_outputs) == 1

        # Add last layer
        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_state,)

        if not return_dict:
            return tuple(v for v in [hidden_state, all_hidden_states, all_attentions] if v is not None)
        return BaseModelOutput(
            last_hidden_state=hidden_state, hidden_states=all_hidden_states, attentions=all_attentions
        )


# INTERFACE FOR ENCODER AND TASK SPECIFIC MODEL #
class DistilRobertaPreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """

    config_class = RobertaConfig
    load_tf_weights = None
    base_model_prefix = "distilroberta"

    def _init_weights(self, module):
        """Initialize the weights."""
        if isinstance(module, nn.Linear):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)

#
# DISTILROBERTA_START_DOCSTRING = r"""
#     This model inherits from :class:`~transformers.PreTrainedModel`. Check the superclass documentation for the generic
#     methods the library implements for all its model (such as downloading or saving, resizing the input embeddings,
#     pruning heads etc.)
#     This model is also a PyTorch `torch.nn.Module <https://pytorch.org/docs/stable/nn.html#torch.nn.Module>`__
#     subclass. Use it as a regular PyTorch Module and refer to the PyTorch documentation for all matter related to
#     general usage and behavior.
#     Parameters:
#         config (:class:`~transformers.DistilBertConfig`): Model configuration class with all the parameters of the model.
#             Initializing with a config file does not load the weights associated with the model, only the
#             configuration. Check out the :meth:`~transformers.PreTrainedModel.from_pretrained` method to load the model
#             weights.
# """
#
# DISTILROBERTA_INPUTS_DOCSTRING = r"""
#     Args:
#         input_ids (:obj:`torch.LongTensor` of shape :obj:`({0})`):
#             Indices of input sequence tokens in the vocabulary.
#             Indices can be obtained using :class:`~transformers.DistilBertTokenizer`. See
#             :meth:`transformers.PreTrainedTokenizer.encode` and :meth:`transformers.PreTrainedTokenizer.__call__` for
#             details.
#             `What are input IDs? <../glossary.html#input-ids>`__
#         attention_mask (:obj:`torch.FloatTensor` of shape :obj:`({0})`, `optional`):
#             Mask to avoid performing attention on padding token indices. Mask values selected in ``[0, 1]``:
#             - 1 for tokens that are **not masked**,
#             - 0 for tokens that are **masked**.
#             `What are attention masks? <../glossary.html#attention-mask>`__
#         head_mask (:obj:`torch.FloatTensor` of shape :obj:`(num_heads,)` or :obj:`(num_layers, num_heads)`, `optional`):
#             Mask to nullify selected heads of the self-attention modules. Mask values selected in ``[0, 1]``:
#             - 1 indicates the head is **not masked**,
#             - 0 indicates the head is **masked**.
#         inputs_embeds (:obj:`torch.FloatTensor` of shape :obj:`({0}, hidden_size)`, `optional`):
#             Optionally, instead of passing :obj:`input_ids` you can choose to directly pass an embedded representation.
#             This is useful if you want more control over how to convert :obj:`input_ids` indices into associated
#             vectors than the model's internal embedding lookup matrix.
#         output_attentions (:obj:`bool`, `optional`):
#             Whether or not to return the attentions tensors of all attention layers. See ``attentions`` under returned
#             tensors for more detail.
#         output_hidden_states (:obj:`bool`, `optional`):
#             Whether or not to return the hidden states of all layers. See ``hidden_states`` under returned tensors for
#             more detail.
#         return_dict (:obj:`bool`, `optional`):
#             Whether or not to return a :class:`~transformers.file_utils.ModelOutput` instead of a plain tuple.
# """


# @add_start_docstrings(
#     "The bare DistilBERT encoder/transformer outputting raw hidden-states without any specific head on top.",
#     DISTILROBERTA_START_DOCSTRING,
# )
class DistilRobertaModel(DistilRobertaPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)

        self.embeddings = Embeddings(config)  # Embeddings
        self.transformer = Transformer(config)  # Encoder

        self.init_weights()

    def get_position_embeddings(self) -> nn.Embedding:
        """
        Returns the position embeddings
        """
        return self.embeddings.position_embeddings

    def resize_position_embeddings(self, new_num_position_embeddings: int):
        """
        Resizes position embeddings of the model if :obj:`new_num_position_embeddings !=
        config.max_position_embeddings`.
        Arguments:
            new_num_position_embeddings (:obj:`int`):
                The number of new position embedding matrix. If position embeddings are learned, increasing the size
                will add newly initialized vectors at the end, whereas reducing the size will remove vectors from the
                end. If position embeddings are not learned (*e.g.* sinusoidal position embeddings), increasing the
                size will add correct vectors at the end following the position encoding algorithm, whereas reducing
                the size will remove vectors from the end.
        """
        num_position_embeds_diff = new_num_position_embeddings - self.config.max_position_embeddings

        # no resizing needs to be done if the length stays the same
        if num_position_embeds_diff == 0:
            return

        logger.info(f"Setting `config.max_position_embeddings={new_num_position_embeddings}`...")
        self.config.max_position_embeddings = new_num_position_embeddings

        old_position_embeddings_weight = self.embeddings.position_embeddings.weight.clone()

        self.embeddings.position_embeddings = nn.Embedding(self.config.max_position_embeddings, self.config.hidden_size)

        if self.config.sinusoidal_pos_embds:
            create_sinusoidal_embeddings(
                n_pos=self.config.max_position_embeddings, dim=self.config.hidden_size,
                out=self.position_embeddings.weight
            )
        else:
            with torch.no_grad():
                if num_position_embeds_diff > 0:
                    self.embeddings.position_embeddings.weight[:-num_position_embeds_diff] = nn.Parameter(
                        old_position_embeddings_weight
                    )
                else:
                    self.embeddings.position_embeddings.weight = nn.Parameter(
                        old_position_embeddings_weight[:num_position_embeds_diff]
                    )
        # move position_embeddings to correct device
        self.embeddings.position_embeddings.to(self.device)

    def get_input_embeddings(self):
        return self.embeddings.word_embeddings

    def set_input_embeddings(self, new_embeddings):
        self.embeddings.word_embeddings = new_embeddings

    def _prune_heads(self, heads_to_prune):
        """
        Prunes heads of the model. heads_to_prune: dict of {layer_num: list of heads to prune in this layer} See base
        class PreTrainedModel
        """
        for layer, heads in heads_to_prune.items():
            self.transformer.layer[layer].attention.prune_heads(heads)

    # @add_start_docstrings_to_model_forward(DISTILROBERTA_INPUTS_DOCSTRING.format("batch_size, num_choices"))
    # @add_code_sample_docstrings(
    #     tokenizer_class=_TOKENIZER_FOR_DOC,
    #     checkpoint=_CHECKPOINT_FOR_DOC,
    #     output_type=BaseModelOutput,
    #     config_class=_CONFIG_FOR_DOC,
    # )
    def forward(
            self,
            input_ids=None,
            attention_mask=None,
            head_mask=None,
            inputs_embeds=None,
            output_attentions=None,
            output_hidden_states=None,
            return_dict=None,
            # for interchange.
            interchanged_variables=None,
            variable_names=None,
            interchange_mask=None,
            dual_interchange_mask=None,
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            input_shape = input_ids.size()
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        if variable_names == "embeddings":
            pass

        device = input_ids.device if input_ids is not None else inputs_embeds.device

        if attention_mask is None:
            attention_mask = torch.ones(input_shape, device=device)  # (bs, seq_length)

        # Prepare head mask if needed
        head_mask = self.get_head_mask(head_mask, self.config.num_hidden_layers)

        if inputs_embeds is None:
            inputs_embeds = self.embeddings.forward(input_ids)  # (bs, seq_length, dim)
        return self.transformer.forward(
            x=inputs_embeds,
            attn_mask=attention_mask,
            head_mask=head_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            interchanged_variables=interchanged_variables,
            variable_names=variable_names,
            interchange_mask=interchange_mask,
            dual_interchange_mask=dual_interchange_mask,
        )


# @add_start_docstrings(
#     """DistilBert Model with a `masked language modeling` head on top. """,
#     DISTILROBERTA_START_DOCSTRING,
# )
class DistilRobertaForMaskedLM(DistilRobertaPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)

        self.distilroberta = DistilRobertaModel(config)
        self.vocab_transform = nn.Linear(config.hidden_size, config.hidden_size)
        self.vocab_layer_norm = nn.LayerNorm(config.hidden_size, eps=1e-12)
        self.vocab_projector = nn.Linear(config.hidden_size, config.vocab_size)

        self.init_weights()

        self.mlm_loss_fct = nn.CrossEntropyLoss()

        # we actually calculate loss here so that it is parallel.
        self.ce_loss_fct = nn.KLDivLoss(reduction="batchmean")
        self.lm_loss_fct = nn.CrossEntropyLoss(ignore_index=-100)
        self.mse_loss_fct = nn.MSELoss(reduction="sum")
        self.cosine_loss_fct = nn.CosineEmbeddingLoss(reduction="mean")

    def get_position_embeddings(self) -> nn.Embedding:
        """
        Returns the position embeddings
        """
        return self.distilroberta.get_position_embeddings()

    def resize_position_embeddings(self, new_num_position_embeddings: int):
        """
        Resizes position embeddings of the model if :obj:`new_num_position_embeddings !=
        config.max_position_embeddings`.
        Arguments:
            new_num_position_embeddings (:obj:`int`):
                The number of new position embedding matrix. If position embeddings are learned, increasing the size
                will add newly initialized vectors at the end, whereas reducing the size will remove vectors from the
                end. If position embeddings are not learned (*e.g.* sinusoidal position embeddings), increasing the
                size will add correct vectors at the end following the position encoding algorithm, whereas reducing
                the size will remove vectors from the end.
        """
        self.distilroberta.resize_position_embeddings(new_num_position_embeddings)

    def get_output_embeddings(self):
        return self.vocab_projector

    def set_output_embeddings(self, new_embeddings):
        self.vocab_projector = new_embeddings

    # @add_start_docstrings_to_model_forward(DISTILROBERTA_INPUTS_DOCSTRING.format("batch_size, num_choices"))
    # @add_code_sample_docstrings(
    #     tokenizer_class=_TOKENIZER_FOR_DOC,
    #     checkpoint=_CHECKPOINT_FOR_DOC,
    #     output_type=MaskedLMOutput,
    #     config_class=_CONFIG_FOR_DOC,
    # )
    def forward(
            self,
            input_ids=None,
            attention_mask=None,
            head_mask=None,
            inputs_embeds=None,
            labels=None,
            output_attentions=None,
            output_hidden_states=None,
            return_dict=None,
            # for interchange.
            interchanged_variables=None,
            variable_names=None,
            interchange_mask=None,
            dual_interchange_mask=None,
            # for calculating the losses.
            t_logits=None,
            t_hidden_states=None,
            causal_t_logits=None,
            causal_t_hidden_states=None,
            s_logits=None,
            s_hidden_states=None,
            temperature=None,
            restrict_ce_to_mask=None,
            lm_labels=None,
            alpha_mlm=0.0,
            alpha_clm=0.0,
            alpha_mse=0.0,
            alpha_cos=0.0,
    ):
        r"""
        labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
            Labels for computing the masked language modeling loss. Indices should be in ``[-100, 0, ...,
            config.vocab_size]`` (see ``input_ids`` docstring) Tokens with indices set to ``-100`` are ignored
            (masked), the loss is only computed for the tokens with labels in ``[0, ..., config.vocab_size]``.
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        dlbrt_output = self.distilroberta.forward(
            input_ids=input_ids,
            attention_mask=attention_mask,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            interchanged_variables=interchanged_variables,
            variable_names=variable_names,
            interchange_mask=interchange_mask,
            dual_interchange_mask=dual_interchange_mask,
        )
        hidden_states = dlbrt_output[0]  # (bs, seq_length, dim)
        prediction_logits = self.vocab_transform(hidden_states)  # (bs, seq_length, dim)
        prediction_logits = gelu(prediction_logits)  # (bs, seq_length, dim)
        prediction_logits = self.vocab_layer_norm(prediction_logits)  # (bs, seq_length, dim)
        prediction_logits = self.vocab_projector(prediction_logits)  # (bs, seq_length, vocab_size)

        mlm_loss = None
        if labels is not None:
            mlm_loss = self.mlm_loss_fct(prediction_logits.view(-1, prediction_logits.size(-1)), labels.view(-1))

        if not return_dict:
            output = (prediction_logits,) + dlbrt_output[1:]
            return ((mlm_loss,) + output) if mlm_loss is not None else output

        student_outputs = MaskedLMOutput(
            loss=mlm_loss,
            logits=prediction_logits,
            hidden_states=dlbrt_output.hidden_states,
            attentions=dlbrt_output.attentions,
        )

        if causal_t_logits is None:
            # if it is None, it is simply a forward for getting hidden states!
            if t_logits is not None:
                assert t_hidden_states is not None
                # regular loss
                s_logits, s_hidden_states = student_outputs["logits"], student_outputs["hidden_states"]
                assert s_logits.size() == t_logits.size()
                # https://github.com/peterliht/knowledge-distillation-pytorch/blob/master/model/net.py#L100
                # https://github.com/peterliht/knowledge-distillation-pytorch/issues/2
                if restrict_ce_to_mask:
                    mask = (lm_labels > -1).unsqueeze(-1).expand_as(s_logits)  # (bs, seq_length, voc_size)
                else:
                    mask = attention_mask.unsqueeze(-1).expand_as(s_logits)  # (bs, seq_length, voc_size)
                s_logits_slct = torch.masked_select(s_logits,
                                                    mask)  # (bs * seq_length * voc_size) modulo the 1s in mask
                s_logits_slct = s_logits_slct.view(-1, s_logits.size(
                    -1))  # (bs * seq_length, voc_size) modulo the 1s in mask
                t_logits_slct = torch.masked_select(t_logits,
                                                    mask)  # (bs * seq_length * voc_size) modulo the 1s in mask
                t_logits_slct = t_logits_slct.view(-1, s_logits.size(
                    -1))  # (bs * seq_length, voc_size) modulo the 1s in mask
                assert t_logits_slct.size() == s_logits_slct.size()

                loss_ce = (
                        self.ce_loss_fct(
                            nn.functional.log_softmax(s_logits_slct / temperature, dim=-1),
                            nn.functional.softmax(t_logits_slct / temperature, dim=-1),
                        )
                        * (temperature) ** 2
                )
                student_outputs["loss_ce"] = loss_ce

                # other distillation loss.
                if alpha_mlm > 0.0:
                    loss_mlm = self.lm_loss_fct(s_logits.view(-1, s_logits.size(-1)), lm_labels.view(-1))
                    student_outputs["loss_mlm"] = loss_mlm
                if alpha_clm > 0.0:
                    shift_logits = s_logits[..., :-1, :].contiguous()
                    shift_labels = lm_labels[..., 1:].contiguous()
                    loss_clm = self.lm_loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))
                    student_outputs["loss_mlm"] = loss_clm
                if alpha_mse > 0.0:
                    loss_mse = self.mse_loss_fct(s_logits_slct, t_logits_slct) / s_logits_slct.size(
                        0
                    )  # Reproducing batchmean reduction
                    student_outputs["loss_mse"] = loss_mse
                if alpha_cos > 0.0:
                    s_hidden_states = s_hidden_states[-1]  # (bs, seq_length, dim)
                    t_hidden_states = t_hidden_states[-1]  # (bs, seq_length, dim)
                    mask = attention_mask.unsqueeze(-1).expand_as(s_hidden_states)  # (bs, seq_length, dim)
                    assert s_hidden_states.size() == t_hidden_states.size()
                    dim = s_hidden_states.size(-1)

                    s_hidden_states_slct = torch.masked_select(s_hidden_states, mask)  # (bs * seq_length * dim)
                    s_hidden_states_slct = s_hidden_states_slct.view(-1, dim)  # (bs * seq_length, dim)
                    t_hidden_states_slct = torch.masked_select(t_hidden_states, mask)  # (bs * seq_length * dim)
                    t_hidden_states_slct = t_hidden_states_slct.view(-1, dim)  # (bs * seq_length, dim)

                    target = s_hidden_states_slct.new(s_hidden_states_slct.size(0)).fill_(1)  # (bs * seq_length,)
                    loss_cos = self.cosine_loss_fct(s_hidden_states_slct, t_hidden_states_slct, target)
                    student_outputs["loss_cos"] = loss_cos
        # causal distillation loss.
        else:
            # if it is None, it is simply a forward for getting hidden states!
            assert t_logits is not None
            assert t_hidden_states is not None
            assert s_logits is not None
            assert s_hidden_states is not None
            assert causal_t_hidden_states is not None

            causal_s_logits, causal_s_hidden_states = \
                student_outputs["logits"], student_outputs["hidden_states"]
            assert causal_s_logits.size() == causal_t_logits.size()
            # https://github.com/peterliht/knowledge-distillation-pytorch/blob/master/model/net.py#L100
            # https://github.com/peterliht/knowledge-distillation-pytorch/issues/2
            if restrict_ce_to_mask:
                causal_mask = (lm_labels > -1).unsqueeze(-1).expand_as(causal_s_logits)  # (bs, seq_length, voc_size)
            else:
                causal_mask = attention_mask.unsqueeze(-1).expand_as(causal_s_logits)  # (bs, seq_length, voc_size)
            causal_s_logits_slct = torch.masked_select(causal_s_logits,
                                                       causal_mask)  # (bs * seq_length * voc_size) modulo the 1s in mask
            causal_s_logits_slct = causal_s_logits_slct.view(-1, causal_s_logits.size(
                -1))  # (bs * seq_length, voc_size) modulo the 1s in mask
            causal_t_logits_slct = torch.masked_select(causal_t_logits,
                                                       causal_mask)  # (bs * seq_length * voc_size) modulo the 1s in mask
            causal_t_logits_slct = causal_t_logits_slct.view(-1, causal_s_logits.size(
                -1))  # (bs * seq_length, voc_size) modulo the 1s in mask
            assert causal_t_logits_slct.size() == causal_s_logits_slct.size()

            causal_loss_ce = (
                    self.ce_loss_fct(
                        nn.functional.log_softmax(causal_s_logits_slct / temperature, dim=-1),
                        nn.functional.softmax(causal_t_logits_slct / temperature, dim=-1),
                    )
                    * (temperature) ** 2
            )
            student_outputs["causal_loss_ce"] = causal_loss_ce

            # now, let us get causal_loss_cos as well.
            s_hidden_states = causal_s_hidden_states[-1]  # (bs, seq_length, dim)
            t_hidden_states = causal_t_hidden_states[-1]  # (bs, seq_length, dim)
            mask = attention_mask.unsqueeze(-1).expand_as(s_hidden_states)  # (bs, seq_length, dim)
            assert s_hidden_states.size() == t_hidden_states.size()
            dim = s_hidden_states.size(-1)

            s_hidden_states_slct = torch.masked_select(s_hidden_states, mask)  # (bs * seq_length * dim)
            s_hidden_states_slct = s_hidden_states_slct.view(-1, dim)  # (bs * seq_length, dim)
            t_hidden_states_slct = torch.masked_select(t_hidden_states, mask)  # (bs * seq_length * dim)
            t_hidden_states_slct = t_hidden_states_slct.view(-1, dim)  # (bs * seq_length, dim)

            target = s_hidden_states_slct.new(s_hidden_states_slct.size(0)).fill_(1)  # (bs * seq_length,)
            causal_loss_cos = self.cosine_loss_fct(s_hidden_states_slct, t_hidden_states_slct, target)
            student_outputs["causal_loss_cos"] = causal_loss_cos

        return student_outputs

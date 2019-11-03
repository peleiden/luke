import torch
from torch import nn
from torch.nn import CrossEntropyLoss
from transformers.modeling_bert import BertPreTrainingHeads, BertPredictionHeadTransform
from transformers.modeling_roberta import RobertaLMHead

from luke.model import LukeModel


class EntityPredictionHead(nn.Module):
    def __init__(self, config):
        super(EntityPredictionHead, self).__init__()
        self.config = config
        self.transform = BertPredictionHeadTransform(config)
        self.decoder = nn.Linear(config.hidden_size, config.entity_vocab_size, bias=False)
        if config.entity_emb_size is not None and config.entity_emb_size != config.hidden_size:
            self.pre_decoder_dense = nn.Linear(config.hidden_size, config.entity_emb_size, bias=False)
        self.bias = nn.Parameter(torch.zeros(config.entity_vocab_size))

    def forward(self, hidden_states):
        hidden_states = self.transform(hidden_states)
        if self.config.entity_emb_size is not None and self.config.entity_emb_size != self.config.hidden_size:
            hidden_states = self.pre_decoder_dense(hidden_states)
        hidden_states = self.decoder(hidden_states) + self.bias

        return hidden_states


class SourceEntityPredictionHead(nn.Module):
    def __init__(self, config):
        super(SourceEntityPredictionHead, self).__init__()
        self.config = config
        self.transform = BertPredictionHeadTransform(config)
        self.decoder = nn.Linear(config.hidden_size, config.entity_vocab_size, bias=False)
        if config.entity_emb_size is not None and config.entity_emb_size != config.hidden_size:
            self.pre_decoder_dense = nn.Linear(config.hidden_size, config.entity_emb_size, bias=False)
        self.bias = nn.Parameter(torch.zeros(config.entity_vocab_size))

    def forward(self, hidden_states):
        hidden_states = self.transform(hidden_states)
        if self.config.entity_emb_size is not None and self.config.entity_emb_size != self.config.hidden_size:
            hidden_states = self.pre_decoder_dense(hidden_states)
        hidden_states = self.decoder(hidden_states) + self.bias

        return hidden_states


class LukePretrainingModel(LukeModel):
    def __init__(self, config):
        super(LukePretrainingModel, self).__init__(config)

        if self.config.bert_model_name and self.config.bert_model_name.startswith('roberta'):
            self.lm_head = RobertaLMHead(config)
            self.lm_head.decoder.weight = self.embeddings.word_embeddings.weight
        else:
            self.cls = BertPreTrainingHeads(config)
            self.cls.predictions.decoder.weight = self.embeddings.word_embeddings.weight

        self.entity_predictions = EntityPredictionHead(config)
        self.entity_predictions.decoder.weight = self.entity_embeddings.entity_embeddings.weight

        self.source_entity_prediction = SourceEntityPredictionHead(config)
        self.source_entity_prediction.decoder.weight = self.entity_embeddings.entity_embeddings.weight

        self.apply(self.init_weights)

    def forward(self, word_ids, word_segment_ids, word_attention_mask, entity_ids, entity_position_ids,
                entity_segment_ids, entity_attention_mask, masked_entity_labels=None, masked_lm_labels=None,
                source_entity_label=None, **kwargs):
        model_dtype = next(self.parameters()).dtype  # for fp16 compatibility

        output = super(LukePretrainingModel, self).forward(
            word_ids, word_segment_ids, word_attention_mask, entity_ids, entity_position_ids, entity_segment_ids,
            entity_attention_mask
        )
        word_sequence_output, entity_sequence_output = output[:2]

        loss_fn = CrossEntropyLoss(ignore_index=-1)
        ret = dict(loss=word_ids.new_tensor(0.0, dtype=model_dtype))

        if masked_entity_labels is not None:
            entity_mask = (masked_entity_labels != -1)
            if entity_mask.sum() > 0:
                target_entity_sequence_output = torch.masked_select(entity_sequence_output, entity_mask.unsqueeze(-1))
                target_entity_sequence_output = target_entity_sequence_output.view(-1, self.config.hidden_size)
                target_entity_labels = torch.masked_select(masked_entity_labels, entity_mask)

                entity_scores = self.entity_predictions(target_entity_sequence_output)
                entity_scores = entity_scores.view(-1, self.config.entity_vocab_size)

                ret['masked_entity_loss'] = loss_fn(entity_scores, target_entity_labels)
                ret['masked_entity_correct'] = (torch.argmax(entity_scores, 1).data == target_entity_labels.data).sum()
                ret['masked_entity_total'] = target_entity_labels.ne(-1).sum()
                ret['loss'] += ret['masked_entity_loss']
            else:
                ret['masked_entity_loss'] = word_ids.new_tensor(0.0, dtype=model_dtype)
                ret['masked_entity_correct'] = word_ids.new_tensor(0, dtype=torch.long)
                ret['masked_entity_total'] = word_ids.new_tensor(0, dtype=torch.long)

        if masked_lm_labels is not None:
            masked_lm_mask = (masked_lm_labels != -1)
            if masked_lm_mask.sum() > 0:
                masked_word_sequence_output = torch.masked_select(word_sequence_output, masked_lm_mask.unsqueeze(-1))
                masked_word_sequence_output = masked_word_sequence_output.view(-1, self.config.hidden_size)

                if self.config.bert_model_name and self.config.bert_model_name.startswith('roberta'):
                    masked_lm_scores = self.lm_head(masked_word_sequence_output)
                else:
                    masked_lm_scores = self.cls.predictions(masked_word_sequence_output)
                masked_lm_scores = masked_lm_scores.view(-1, self.config.vocab_size)
                masked_lm_labels = torch.masked_select(masked_lm_labels, masked_lm_mask)

                ret['masked_lm_loss'] = loss_fn(masked_lm_scores, masked_lm_labels)
                ret['masked_lm_correct'] = (torch.argmax(masked_lm_scores, 1).data == masked_lm_labels.data).sum()
                ret['masked_lm_total'] = masked_lm_labels.ne(-1).sum()
                ret['loss'] += ret['masked_lm_loss']
            else:
                ret['masked_lm_loss'] = word_ids.new_tensor(0.0, dtype=model_dtype)
                ret['masked_lm_correct'] = word_ids.new_tensor(0, dtype=torch.long)
                ret['masked_lm_total'] = word_ids.new_tensor(0, dtype=torch.long)

        if source_entity_label is not None:
            if source_entity_label.ne(-1).sum() > 0:
                source_entity_score = self.source_entity_prediction(word_sequence_output[:, 0])
                ret['source_entity_loss'] = loss_fn(source_entity_score, source_entity_label)
                ret['source_entity_correct'] = (torch.argmax(source_entity_score, 1).data ==
                                                source_entity_label.data).sum()
                ret['source_entity_total'] = source_entity_label.ne(-1).sum()
                ret['loss'] += ret['source_entity_loss']
            else:
                ret['source_entity_loss'] = word_ids.new_tensor(0.0, dtype=model_dtype)
                ret['source_entity_correct'] = word_ids.new_tensor(0, dtype=torch.long)
                ret['source_entity_total'] = word_ids.new_tensor(0, dtype=torch.long)

        return ret
# coding=utf-8
# Copyright 2020 The Edward2 Authors.
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

"""Ensemble on CLINC Intent Detection.

This script only performs evaluation, not training. We recommend training
ensembles by launching independent runs of `deterministic.py` over different
seeds.
"""

import os

from absl import app
from absl import flags
from absl import logging

import edward2 as ed
# import clinc_intent.deterministic to inhere its flags
import deterministic  # pylint:disable=unused-import  # local file import
import deterministic_model as cnn_model  # local file import
import deterministic_model_bert as bert_model  # local file import

import numpy as np
import tensorflow.compat.v2 as tf
import uncertainty_baselines as ub

# TODO(trandustin): We inherit
# FLAGS.{dataset,per_core_batch_size,output_dir,seed} from deterministic. This
# is not intuitive, which suggests we need to either refactor to avoid importing
# from a binary or duplicate the model definition here.

# Model flags
flags.DEFINE_string('checkpoint_dir', None,
                    'The directory where the model weights are stored.')
flags.mark_flag_as_required('checkpoint_dir')
FLAGS = flags.FLAGS


def ensemble_negative_log_likelihood(labels, logits):
  """Negative log-likelihood for ensemble.

  For each datapoint (x,y), the ensemble's negative log-likelihood is:

  ```
  -log p(y|x) = -log sum_{m=1}^{ensemble_size} exp(log p(y|x,theta_m)) +
                log ensemble_size.
  ```

  Args:
    labels: tf.Tensor of shape [...].
    logits: tf.Tensor of shape [ensemble_size, ..., num_classes].

  Returns:
    tf.Tensor of shape [...].
  """
  labels = tf.cast(labels, tf.int32)
  logits = tf.convert_to_tensor(logits)
  ensemble_size = float(logits.shape[0])
  nll = tf.nn.sparse_softmax_cross_entropy_with_logits(
      tf.broadcast_to(labels[tf.newaxis, ...],
                      tf.shape(logits)[:-1]), logits)
  return -tf.reduce_logsumexp(-nll, axis=0) + tf.math.log(ensemble_size)


def gibbs_cross_entropy(labels, logits):
  """Average cross entropy for ensemble members (Gibbs cross entropy).

  For each datapoint (x,y), the ensemble's Gibbs cross entropy is:

  ```
  GCE = - (1/ensemble_size) sum_{m=1}^ensemble_size log p(y|x,theta_m).
  ```

  The Gibbs cross entropy approximates the average cross entropy of a single
  model drawn from the (Gibbs) ensemble.

  Args:
    labels: tf.Tensor of shape [...].
    logits: tf.Tensor of shape [ensemble_size, ..., num_classes].

  Returns:
    tf.Tensor of shape [...].
  """
  labels = tf.cast(labels, tf.int32)
  logits = tf.convert_to_tensor(logits)
  nll = tf.nn.sparse_softmax_cross_entropy_with_logits(
      tf.broadcast_to(labels[tf.newaxis, ...],
                      tf.shape(logits)[:-1]), logits)
  return tf.reduce_mean(nll, axis=0)


def main(argv):
  del argv  # unused arg
  if not FLAGS.use_gpu:
    raise ValueError('Only GPU is currently supported.')
  if FLAGS.num_cores > 1:
    raise ValueError('Only a single accelerator is currently supported.')
  tf.enable_v2_behavior()
  tf.random.set_seed(FLAGS.seed)
  tf.io.gfile.makedirs(FLAGS.output_dir)

  ind_dataset_builder = ub.datasets.ClincIntentDetectionDataset(
      batch_size=FLAGS.per_core_batch_size,
      eval_batch_size=FLAGS.per_core_batch_size,
      dataset_dir=FLAGS.dataset_dir,
      data_mode='ind')
  ood_dataset_builder = ub.datasets.ClincIntentDetectionDataset(
      batch_size=FLAGS.per_core_batch_size,
      eval_batch_size=FLAGS.per_core_batch_size,
      dataset_dir=FLAGS.dataset_dir,
      data_mode='ood')

  dataset_builders = {'clean': ind_dataset_builder, 'ood': ood_dataset_builder}

  ds_info = ind_dataset_builder.info
  feature_size = ds_info['feature_size']
  # num_classes is number of valid intents plus out-of-scope intent
  num_classes = ds_info['num_classes'] + 1
  # vocab_size is total number of valid tokens plus the out-of-vocabulary token.
  vocab_size = ind_dataset_builder.tokenizer.num_words + 1

  batch_size = FLAGS.per_core_batch_size * FLAGS.num_cores

  test_datasets = {}
  steps_per_eval = {}
  for dataset_name, dataset_builder in dataset_builders.items():
    test_datasets[dataset_name] = dataset_builder.build(
        split=ub.datasets.base.Split.TEST)
    steps_per_eval[dataset_name] = (
        dataset_builder.info['num_test_examples'] // batch_size)

  if FLAGS.model_family.lower() == 'textcnn':
    model = cnn_model.textcnn(
        filter_sizes=FLAGS.filter_sizes,
        num_filters=FLAGS.num_filters,
        num_classes=num_classes,
        feature_size=feature_size,
        vocab_size=vocab_size,
        embed_size=FLAGS.embedding_size,
        dropout_rate=FLAGS.dropout_rate,
        l2=FLAGS.l2)
  elif FLAGS.model_family.lower() == 'bert':
    bert_config_dir, _ = deterministic.resolve_bert_ckpt_and_config_dir(
        FLAGS.bert_dir, FLAGS.bert_config_dir, FLAGS.bert_ckpt_dir)
    bert_config = bert_model.create_config(bert_config_dir)
    model, _ = bert_model.create_model(num_classes=num_classes,
                                       feature_size=feature_size,
                                       bert_config=bert_config)
  else:
    raise ValueError('model_family ({}) can only be TextCNN or BERT.'.format(
        FLAGS.model_family))

  logging.info('Model input shape: %s', model.input_shape)
  logging.info('Model output shape: %s', model.output_shape)
  logging.info('Model number of weights: %s', model.count_params())

  # Search for checkpoints from their index file; then remove the index suffix.
  ensemble_filenames = tf.io.gfile.glob(
      os.path.join(FLAGS.checkpoint_dir, '**/*.index'))
  ensemble_filenames = [filename[:-6] for filename in ensemble_filenames]
  ensemble_size = len(ensemble_filenames)
  logging.info('Ensemble size: %s', ensemble_size)
  logging.info('Ensemble number of weights: %s',
               ensemble_size * model.count_params())
  logging.info('Ensemble filenames: %s', str(ensemble_filenames))
  checkpoint = tf.train.Checkpoint(model=model)

  # Write model predictions to files.
  num_datasets = len(test_datasets)
  for m, ensemble_filename in enumerate(ensemble_filenames):
    checkpoint.restore(ensemble_filename)
    for n, (name, test_dataset) in enumerate(test_datasets.items()):
      filename = '{dataset}_{member}.npy'.format(dataset=name, member=m)
      filename = os.path.join(FLAGS.output_dir, filename)
      if not tf.io.gfile.exists(filename):
        logits = []
        test_iterator = iter(test_dataset)
        for _ in range(steps_per_eval[name]):
          inputs = next(test_iterator)
          features, _ = deterministic.create_feature_and_label(
              inputs, feature_size, model_family=FLAGS.model_family)
          logits.append(model(features, training=False))

        logits = tf.concat(logits, axis=0)
        with tf.io.gfile.GFile(filename, 'w') as f:
          np.save(f, logits.numpy())
      percent = (m * num_datasets + (n + 1)) / (ensemble_size * num_datasets)
      message = ('{:.1%} completion for prediction: ensemble member {:d}/{:d}. '
                 'Dataset {:d}/{:d}'.format(percent, m + 1, ensemble_size,
                                            n + 1, num_datasets))
      logging.info(message)

  metrics = {
      'test/negative_log_likelihood': tf.keras.metrics.Mean(),
      'test/gibbs_cross_entropy': tf.keras.metrics.Mean(),
      'test/accuracy': tf.keras.metrics.SparseCategoricalAccuracy(),
      'test/ece': ed.metrics.ExpectedCalibrationError(num_bins=FLAGS.num_bins),
  }

  for dataset_name, test_dataset in test_datasets.items():
    if dataset_name != 'clean':
      metrics.update({
          'test/nll_{}'.format(dataset_name):
              tf.keras.metrics.Mean(),
          'test/accuracy_{}'.format(dataset_name):
              tf.keras.metrics.SparseCategoricalAccuracy(),
          'test/ece_{}'.format(dataset_name):
              ed.metrics.ExpectedCalibrationError(num_bins=FLAGS.num_bins)
      })

  # Evaluate model predictions.
  for n, (name, test_dataset) in enumerate(test_datasets.items()):
    logits_dataset = []
    for m in range(ensemble_size):
      filename = '{dataset}_{member}.npy'.format(dataset=name, member=m)
      filename = os.path.join(FLAGS.output_dir, filename)
      with tf.io.gfile.GFile(filename, 'rb') as f:
        logits_dataset.append(np.load(f))

    logits_dataset = tf.convert_to_tensor(logits_dataset)
    test_iterator = iter(test_dataset)
    for step in range(steps_per_eval[name]):
      inputs = next(test_iterator)
      _, labels = deterministic.create_feature_and_label(
          inputs, feature_size, model_family=FLAGS.model_family)
      logits = logits_dataset[:, (step * batch_size):((step + 1) * batch_size)]
      labels = tf.cast(labels, tf.int32)
      negative_log_likelihood = tf.reduce_mean(
          ensemble_negative_log_likelihood(labels, logits))
      per_probs = tf.nn.softmax(logits)
      probs = tf.reduce_mean(per_probs, axis=0)
      if name == 'clean':
        gibbs_ce = tf.reduce_mean(gibbs_cross_entropy(labels, logits))
        metrics['test/negative_log_likelihood'].update_state(
            negative_log_likelihood)
        metrics['test/gibbs_cross_entropy'].update_state(gibbs_ce)
        metrics['test/accuracy'].update_state(labels, probs)
        metrics['test/ece'].update_state(labels, probs)
      else:
        metrics['test/nll_{}'.format(name)].update_state(
            negative_log_likelihood)
        metrics['test/accuracy_{}'.format(name)].update_state(
            labels, probs)
        metrics['test/ece_{}'.format(name)].update_state(labels, probs)

    message = ('{:.1%} completion for evaluation: dataset {:d}/{:d}'.format(
        (n + 1) / num_datasets, n + 1, num_datasets))
    logging.info(message)

  total_results = {name: metric.result() for name, metric in metrics.items()}
  logging.info('Metrics: %s', total_results)


if __name__ == '__main__':
  app.run(main)

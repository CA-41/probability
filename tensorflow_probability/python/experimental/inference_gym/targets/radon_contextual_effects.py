# Lint as: python2, python3
# Copyright 2020 The TensorFlow Probability Authors.
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
"""Radon contextual effects model."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools

import tensorflow.compat.v2 as tf
from tensorflow_probability.python import bijectors as tfb
from tensorflow_probability.python import distributions as tfd

from tensorflow_probability.python.experimental.inference_gym.internal import data
from tensorflow_probability.python.experimental.inference_gym.targets import bayesian_model
from tensorflow_probability.python.experimental.inference_gym.targets import model

__all__ = [
    'RadonContextualEffects',
    'RadonContextualEffectsMinnesota'
]


@tf.function
def affine(x, kernel_diag, bias=None):
  """`kernel_diag * x + bias` with broadcasting."""
  if bias is None:
    bias = tf.zeros([], dtype=x.dtype)
  return x * kernel_diag + bias


def make_radon_prior(num_counties, dtype):
  """Generative process for the radon model with contextual effects."""
  return tfd.JointDistributionNamed(
      dict(
          county_effect_mean=tfd.Normal(
              loc=tf.zeros([], dtype=dtype), scale=1.),
          county_effect_scale=tfd.Uniform(
              low=tf.zeros([], dtype=dtype), high=100.),
          county_effect=(
              lambda county_effect_scale, county_effect_mean:  # pylint: disable=g-long-lambda
              tfd.Sample(
                  tfd.Normal(loc=county_effect_mean, scale=county_effect_scale),
                  sample_shape=[num_counties])),
          weight=tfd.Sample(
              tfd.Normal(loc=tf.zeros([], dtype=dtype), scale=1.),
              sample_shape=[3]),
          log_radon_scale=tfd.Uniform(low=tf.zeros([], dtype=dtype), high=100.),
          ))


def make_radon_observation_dist(
    params, log_uranium, floor, county, floor_by_county):
  """Likelihood of observed data under the contextual effects radon model."""
  floor = tf.cast(floor, dtype=log_uranium.dtype)
  return tfd.Normal(
      loc=affine(
          log_uranium,
          params['weight'][..., :1],
          affine(floor, params['weight'][..., 1:2])
          + affine(floor_by_county, params['weight'][..., 2:])
          + tf.gather(params['county_effect'], county, axis=-1)),
      scale=params['log_radon_scale'][..., tf.newaxis])


def radon_log_likelihood_fn(
    params, log_uranium, floor, county, floor_by_county, log_radon,
    reduce_sum=True):
  log_likelihood = make_radon_observation_dist(
      params, log_uranium, floor, county, floor_by_county).log_prob(log_radon)
  if reduce_sum:
    return tf.reduce_sum(log_likelihood, [-1])
  return log_likelihood


class RadonContextualEffects(bayesian_model.BayesianModel):
  """Hierarchical radon model with contextual effects.

  Radon is a radioactive gas that enters homes through contact points with the
  ground. It is a carcinogen that is the primary cause of lung cancer in
  non-smokers. Radon levels vary greatly from household to household.

  The EPA did a study of radon levels in 80,000 houses. Two important predictors
  are: 1. Measurement in the basement or the first floor (radon higher in
  basements) 2. County uranium level (positive correlation with radon levels)
  We will build a hierarchical model of radon measurements in houses, in which
  the hierarchy is households within each county. We will incorporate a
  contextual effect corresponding to the mean floor at which the measurement
  was taken, by county.

  ```none

  county_effect_mean ~ Normal(loc=0., scale=1.)
  county_effect_scale ~ Uniform(low=0., high=100.)
  county_effect ~ Sample(
      Normal(loc=county_effect_mean, scale=county_effect_scale),
      sample_shape=[num_counties])

  weight ~ Sample(Normal(loc=0., scale=1.), sample_shape=[3])
  log_radon_scale ~ Uniform(low=0., high=100.)

  log_radon ~ Normal(
      loc=log_uranium * weight[1]           # effect of soil uranium
          + floor * weight[2]               # effect of floor
          + floor_by_county * weight[3]     # effect of mean floor by county
          + gather(county_effect, county),  # effect of county
      scale=log_radon_scale)
  ```

  This model is based on an example from [1] and is the same as the Stan model
  at
  https://mc-stan.org/users/documentation/case-studies/radon.html#Correlations-among-levels

  #### References

  [1] Gelman, A., & Hill, J. (2007). Data Analysis Using Regression and
      Multilevel/Hierarchical Models (1st ed.). Cambridge University Press.
  [2] Stan Development Team. 2018. Stan Modeling Language Users Guide and
      Reference Manual, Version 2.18.0. http://mc-stan.org
  """

  def __init__(
      self,
      num_counties,
      train_log_uranium,
      train_floor,
      train_county,
      train_floor_by_county,
      train_log_radon,
      test_log_uranium=None,
      test_floor=None,
      test_county=None,
      test_floor_by_county=None,
      test_log_radon=None,
      name='radon_contextual_effects',
      pretty_name='Radon Contextual Effects'):
    """Construct the hierarchical radon model with contextual effects.

    Args:
      num_counties: `int`, number of counties represented in the data.
      train_log_uranium: Floating-point `Tensor` with shape
        `[num_train_points]`. Soil uranium measurements.
      train_floor: Integer `Tensor` with shape `[num_train_points]`. Floor of
        the house on which the measurement was taken.
      train_county: Integer `Tensor` with values in `range(0, num_counties)` of
        shape `[num_train_points]`. County in which the measurement was taken.
      train_floor_by_county: Floating-point `Tensor` with shape
        `[num_train_points]`. Average floor on which the measurement was taken
        for the county in which each house is located (the `Tensor` will have
        `num_counties` unique values). This represents the contextual effect.
      train_log_radon: Floating-point `Tensor` with shape `[num_train_points]`.
        Radon measurement for each house (the dependent variable in the model).
      test_log_uranium: Floating-point `Tensor` with shape `[num_test_points]`.
        Soil uranium measurements for the test set. Can be `None`, in which case
        test-related sample transformations are not computed.
      test_floor: Integer `Tensor` with shape `[num_test_points]`. Floor of the
        house on which the measurement was taken. Can be `None`, in which case
        test-related sample transformations are not computed.
      test_county: Integer `Tensor` with values in `range(0, num_counties)` of
        shape `[num_test_points]`. County in which the measurement was taken.
        Can be `None`, in which case test-related sample transformations are not
        computed.
      test_floor_by_county: Floating-point `Tensor` with shape
        `[num_test_points]`. Average floor on which the measurement was taken
        (calculated from the training set) for the county in which each house is
        located (the `Tensor` will have `num_counties` unique values). This
        represents the contextual effect. Can be `None`, in which case
        test-related sample transformations are not computed.
      test_log_radon: Floating-point `Tensor` with shape `[num_test_points]`.
        Radon measurement for each house (the dependent variable in the model).
        Can be `None`, in which case test-related sample transformations are not
        computed.
      name: Python `str` name prefixed to Ops created by this class.
      pretty_name: A Python `str`. The pretty name of this model.

    Raises:
      ValueError if any but not all of `test_*` inputs are None.
    """
    with tf.name_scope(name):
      test_data = (test_log_uranium, test_floor, test_county,
                   test_floor_by_county, test_log_radon)
      test_data_present = (d is not None for d in test_data)
      self._have_test = all(test_data_present)
      if not self._have_test and any(test_data_present):
        raise ValueError(
            'Test set values must all be specified or all `None`. Got:'
            '`test_log_uranium`={}, `test_floor`={}, `test_county`={},'
            '`test_floor_by_county`={}, `test_log_radon`={}`'.format(
                *test_data))

      dtype = train_log_radon.dtype
      self._prior_dist = make_radon_prior(num_counties, dtype=dtype)

      self._train_log_likelihood_fn = functools.partial(
          radon_log_likelihood_fn,
          log_uranium=train_log_uranium,
          floor=train_floor,
          county=train_county,
          floor_by_county=train_floor_by_county,
          log_radon=train_log_radon)

      sample_transformations = {
          'identity':
              model.Model.SampleTransformation(
                  fn=lambda params: params,
                  pretty_name='Identity',
                  dtype=dtype
              )
      }

      if self._have_test:
        test_log_likelihood_fn = functools.partial(
            radon_log_likelihood_fn,
            log_uranium=test_log_uranium,
            floor=test_floor,
            county=test_county,
            floor_by_county=test_floor_by_county,
            log_radon=test_log_radon)

        sample_transformations['test_nll'] = (
            model.Model.SampleTransformation(
                fn=test_log_likelihood_fn,
                pretty_name='Test NLL',
            ))
        sample_transformations['per_example_test_nll'] = (
            model.Model.SampleTransformation(
                fn=functools.partial(test_log_likelihood_fn, reduce_sum=False),
                pretty_name='Per-example Test NLL',
            ))

      self._train_log_uranium = train_log_uranium
      self._train_floor = train_floor
      self._train_county = train_county
      self._train_floor_by_county = train_floor_by_county
      self._test_log_uranium = test_log_uranium
      self._test_floor = test_floor
      self._test_county = test_county
      self._test_floor_by_county = test_floor_by_county
      self._num_counties = num_counties

    super(RadonContextualEffects, self).__init__(
        default_event_space_bijector={
            'county_effect_mean': tfb.Identity(),
            'county_effect_scale': tfb.Sigmoid(
                low=tf.zeros([], dtype=dtype), high=100.),
            'county_effect': tfb.Identity(),
            'weight': tfb.Identity(),
            'log_radon_scale': tfb.Sigmoid(
                low=tf.zeros([], dtype=dtype), high=100.)
        },
        event_shape=self._prior_dist.event_shape,
        dtype=self._prior_dist.dtype,
        name=name,
        pretty_name=pretty_name,
        sample_transformations=sample_transformations,
    )

  def _prior_distribution(self):
    return self._prior_dist

  def _log_likelihood(self, value):
    return self._train_log_likelihood_fn(value)

  def _sample_dataset(self, seed):
    dataset = dict(
        train_log_uranium=self._train_log_uranium,
        train_floor=self._train_floor,
        train_county=self._train_county,
        train_floor_by_county=self._train_floor_by_county,
        num_counties=self._num_counties,
        test_log_uranium=self._test_log_uranium,
        test_floor=self._test_floor,
        test_county=self._test_county,
        test_floor_by_county=self._test_floor_by_county,
        )
    prior_samples = self._prior_distribution().sample(seed=seed)
    dist = make_radon_observation_dist(
        prior_samples, self._train_log_uranium, self._train_floor,
        self._train_county, self._train_floor_by_county)
    dataset['train_log_radon'] = dist.sample(seed=seed)
    if self._have_test:
      test_dist = make_radon_observation_dist(
          prior_samples, self._test_log_uranium, self._test_floor,
          self._test_county, self._test_floor_by_county)
      dataset['test_log_radon'] = test_dist.sample(seed=seed)
    return dataset


class RadonContextualEffectsMinnesota(RadonContextualEffects):
  """Bayesian hierarchical model to predict radon measurements in houses.

  This model uses the Radon data set that accompanies the example in [1],
  filtered to include only houses in Minnesota.

  #### References

  [1] Gelman, A., & Hill, J. (2007). Data Analysis Using Regression and
      Multilevel/Hierarchical Models (1st ed.). Cambridge University Press.
      http://www.stat.columbia.edu/~gelman/arm/examples/radon/
  """

  def __init__(self):
    dataset = data.radon(state='MN')
    for key in list(dataset.keys()):
      if key.startswith('test_'):
        del dataset[key]
    super(RadonContextualEffectsMinnesota, self).__init__(
        name='radon_contextual_effects_minnesota',
        pretty_name='Radon Contextual Effects Minnesota',
        **dataset)

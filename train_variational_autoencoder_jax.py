"""Train variational autoencoder or binary MNIST data.

Largely follows https://github.com/deepmind/dm-haiku/blob/master/examples/vae.py"""

import time
import argparse
import pathlib
from calendar import c
from typing import Generator, Mapping, NamedTuple, Sequence, Tuple
from distrax import Inverse

import numpy as np
import jax
import haiku as hk
import jax.numpy as jnp
import optax
import tensorflow_datasets as tfds
from tensorflow_probability.substrates import jax as tfp

tfd = tfp.distributions
tfb = tfp.bijectors

Batch = Mapping[str, np.ndarray]
MNIST_IMAGE_SHAPE: Sequence[int] = (28, 28, 1)
PRNGKey = jnp.ndarray
Array = jnp.ndarray


def add_args(parser):
    parser.add_argument("--latent_size", type=int, default=128)
    parser.add_argument("--hidden_size", type=int, default=512)
    parser.add_argument("--learning_rate", type=float, default=0.001)
    parser.add_argument("--batch_size", type=int, default=128)
    parser.add_argument("--training_steps", type=int, default=30000)
    parser.add_argument("--log_interval", type=int, default=10000)
    parser.add_argument("--num_importance_samples", type=int, default=1000)
    parser.add_argument("--gpu", default=False, action=argparse.BooleanOptionalAction)
    parser.add_argument("--random_seed", type=int, default=42)


def load_dataset(
    split: str, batch_size: int, seed: int, repeat: bool = False
) -> Generator[Batch, None, None]:
    ds = tfds.load(
        "binarized_mnist",
        split=split,
        shuffle_files=True,
        read_config=tfds.ReadConfig(shuffle_seed=seed),
    )
    ds = ds.shuffle(buffer_size=10 * batch_size, seed=seed)
    ds = ds.batch(batch_size)
    ds = ds.prefetch(buffer_size=5)
    if repeat:
        ds = ds.repeat()
    return iter(tfds.as_numpy(ds))


class Model(hk.Module):
    """Deep latent Gaussian model or variational autoencoder."""

    def __init__(
        self,
        latent_size: int,
        hidden_size: int,
        output_shape: Sequence[int] = MNIST_IMAGE_SHAPE,
    ):
        super().__init__(name="model")
        self._latent_size = latent_size
        self._hidden_size = hidden_size
        self._output_shape = output_shape
        self.generative_network = hk.Sequential(
            [
                hk.Linear(self._hidden_size),
                jax.nn.relu,
                hk.Linear(self._hidden_size),
                jax.nn.relu,
                hk.Linear(np.prod(self._output_shape)),
                hk.Reshape(self._output_shape, preserve_dims=2),
            ]
        )

    def __call__(self, x: Array, z: Array) -> Array:
        """Compute log probability"""
        p_z = tfd.Normal(
            loc=jnp.zeros(self._latent_size, dtype=jnp.float32), scale=jnp.ones(self._latent_size, dtype=jnp.float32)
        )
        # sum over latent dimensions
        log_p_z = p_z.log_prob(z).sum(-1)
        logits = self.generative_network(z)
        p_x_given_z = tfd.Bernoulli(logits=logits)
        # sum over last three image dimensions (width, height, channels)
        log_p_x_given_z = p_x_given_z.log_prob(x).sum(axis=(-3, -2, -1))
        return log_p_z + log_p_x_given_z


class VariationalMeanField(hk.Module):
    """Mean field variational distribution q(z | x) parameterized by inference network."""

    def __init__(self, latent_size: int, hidden_size: int):
        super().__init__(name="variational")
        self._latent_size = latent_size
        self._hidden_size = hidden_size
        self.inference_network = hk.Sequential(
            [
                hk.Flatten(),
                hk.Linear(self._hidden_size),
                jax.nn.relu,
                hk.Linear(self._hidden_size),
                jax.nn.relu,
                hk.Linear(self._latent_size * 2),
            ]
        )

    def condition(self, inputs):
        """Compute parameters of a multivariate independent Normal distribution based on the inputs."""
        out = self.inference_network(inputs)
        loc, scale_arg = jnp.split(out, 2, axis=-1)
        scale = jax.nn.softplus(scale_arg)
        return loc, scale

    def __call__(self, x: Array, num_samples: int) -> Tuple[Array, Array]:
        """Compute sample and log probability"""
        loc, scale = self.condition(x)
        # IMPORTANT: need to check in source code that reparameterization_type=tfd.FULLY_REPARAMETERIZED for this class
        q_z = tfd.Normal(loc=loc, scale=scale)
        z = q_z.sample(sample_shape=[num_samples], seed=hk.next_rng_key())
        # sum over latent dimension
        log_q_z = q_z.log_prob(z).sum(-1)
        return z, log_q_z


def make_conditioner(
    event_shape: Sequence[int], hidden_sizes: Sequence[int], num_bijector_params: int
) -> hk.Sequential:
    """Creates an MLP conditioner for each layer of the flow."""
    return hk.Sequential(
        [
            hk.Flatten(preserve_dims=-len(event_shape)),
            hk.nets.MLP(hidden_sizes, activate_final=True),
            # We initialize this linear layer to zero so that the flow is initialized
            # to the identity function.
            hk.Linear(
                np.prod(event_shape) * num_bijector_params,
                w_init=jnp.zeros,
                b_init=jnp.zeros,
            ),
            hk.Reshape(tuple(event_shape) + (num_bijector_params,), preserve_dims=-1),
        ]
    )


class FlowSequential(hk.Sequential):
    def __call__(self, inputs, *args, **kwargs):
        """Calls all layers sequentially to compute sample and log probability."""
        total_log_prob = jnp.zeros_like(inputs)
        out = inputs
        for i, layer in enumerate(self.layers):
            if i == 0:
                out, log_prob = layer(out, *args, **kwargs)
            else:
                out = layer(out)
            total_log_prob += log_prob
        return out, total_log_prob


class VariationalFlow(hk.Module):
    """Uses masked autoregressive networks and a shift scale transform.

    Follows Algorithm 1 from the Inverse Autoregressive Flow paper, Kingma et al. (2016) https://arxiv.org/abs/1606.04934.
    """

    def __init__(self, latent_size: int, hidden_size: int):
        super().__init__(name="variational")
        self._latent_size = latent_size
        self._hidden_size = hidden_size
        self.encoder = hk.nets.MLP(
            output_sizes=[hidden_size, hidden_size, latent_size * 3],
            activation=jax.nn.relu,
            activate_final=False,
        )
        self.first_block = InverseAutoregressiveFlow(latent_size, hidden_size)
        self.second_block = InverseAutoregressiveFlow(latent_size, hidden_size)

    def __call__(self, x: Array, num_samples: int) -> Tuple[Array, Array]:
        """Compute sample and log probability."""
        loc, scale_arg, h = jnp.split(self.encoder(x), 3, axis=-1)
        q_z0 = tfd.Normal(loc=loc, scale=jax.nn.softplus(scale_arg))
        z0 = q_z0.sample(sample_shape=[num_samples], seed=hk.next_rng_key())
        log_q_z0 = q_z0.log_prob(z0).sum(-1)
        z1, log_det_q_z1 = self.first_block(z0, context=h)
        z2, log_det_q_z2 = self.second_block(z1, context=h)
        return z2, log_q_z0 + log_det_q_z1 + log_det_q_z2


class InverseAutoregressiveFlow(hk.Module):
    def __init__(self, latent_size: int, hidden_size: int):
        super().__init__()
        self.made = tfb.AutoregressiveNetwork(
            params=latent_size,
            hidden_units=[hidden_size, hidden_size],
            conditional=True,
            conditional_event_shape=latent_size,
            activation=jax.nn.relu,
        )

    def __call__(self, input: Array, context: Array):
        m, s = jnp.split(self.made(input, conditional_input=context), 2, axis=-1)
        sigmoid = jax.nn.sigmoid(s)
        z = sigmoid * input + (1 - sigmoid) * m
        return z, -jax.nn.log_sigmoid(s).sum(-1)


def main():
    start_time = time.time()
    parser = argparse.ArgumentParser()
    add_args(parser)
    args = parser.parse_args()
    print(args)
    print("jax_disable_jit: ", jax.config.read('jax_disable_jit'))
    rng_seq = hk.PRNGSequence(args.random_seed)
    p_log_prob = hk.transform(
        lambda x, z: Model(args.latent_size, args.hidden_size, MNIST_IMAGE_SHAPE)(
            x=x, z=z
        )
    )
    q_sample_and_log_prob = hk.transform(
        lambda x, num_samples: VariationalMeanField(args.latent_size, args.hidden_size)(
            x, num_samples
        )
    )
    p_params = p_log_prob.init(
        next(rng_seq),
        z=np.zeros((1, args.latent_size), dtype=np.float32),
        x=np.zeros((1, *MNIST_IMAGE_SHAPE), dtype=np.float32),
    )
    q_params = q_sample_and_log_prob.init(
        next(rng_seq), 
        x=np.zeros((1, *MNIST_IMAGE_SHAPE), dtype=np.float32), 
        num_samples=1
    )
    optimizer = optax.rmsprop(args.learning_rate)
    params = (p_params, q_params)
    opt_state = optimizer.init(params)

    @jax.jit
    def objective_fn(params: hk.Params, rng_key: PRNGKey, batch: Batch) -> Array:
        """Objective function is negative ELBO."""
        x = batch["image"]
        p_params, q_params = params
        z, log_q_z = q_sample_and_log_prob.apply(q_params, rng_key, x=x, num_samples=1)
        log_p_x_z = p_log_prob.apply(p_params, rng_key, x=x, z=z)
        elbo = log_p_x_z - log_q_z
        # average elbo over number of samples
        elbo = elbo.mean(axis=0)
        # sum elbo over batch
        elbo = elbo.sum(axis=0)
        return -elbo

    @jax.jit
    def train_step(
        params: hk.Params, rng_key: PRNGKey, opt_state: optax.OptState, batch: Batch
    ) -> Tuple[hk.Params, optax.OptState]:
        """Single update step to maximize the ELBO."""
        grads = jax.grad(objective_fn)(params, rng_key, batch)
        updates, new_opt_state = optimizer.update(grads, opt_state)
        new_params = optax.apply_updates(params, updates)
        return new_params, new_opt_state

    @jax.jit
    def importance_weighted_estimate(
        params: hk.Params, rng_key: PRNGKey, batch: Batch
    ) -> Tuple[Array, Array]:
        """Estimate marginal log p(x) using importance sampling."""
        x = batch["image"]
        p_params, q_params = params
        z, log_q_z = q_sample_and_log_prob.apply(
            q_params, rng_key, x=x, num_samples=args.num_importance_samples
        )
        log_p_x_z = p_log_prob.apply(p_params, rng_key, x, z)
        elbo = log_p_x_z - log_q_z
        # importance sampling of approximate marginal likelihood with q(z)
        # as the proposal, and logsumexp in the sample dimension
        log_p_x = jax.nn.logsumexp(elbo, axis=0) - jnp.log(jnp.shape(elbo)[0])
        # sum over the elements of the minibatch
        log_p_x = log_p_x.sum(0)
        # average elbo over number of samples
        elbo = elbo.mean(axis=0)
        # sum elbo over batch
        elbo = elbo.sum(axis=0)
        return elbo, log_p_x

    def evaluate(
        dataset: Generator[Batch, None, None],
        params: hk.Params,
        rng_seq: hk.PRNGSequence,
    ) -> Tuple[float, float]:
        total_elbo = 0.0
        total_log_p_x = 0.0
        dataset_size = 0
        for batch in dataset:
            elbo, log_p_x = importance_weighted_estimate(params, next(rng_seq), batch)
            total_elbo += elbo
            total_log_p_x += log_p_x
            dataset_size += len(batch["image"])
        return total_elbo / dataset_size, total_log_p_x / dataset_size

    train_ds = load_dataset(
        tfds.Split.TRAIN, args.batch_size, args.random_seed, repeat=True
    )
    test_ds = load_dataset(tfds.Split.TEST, args.batch_size, args.random_seed)

    def print_progress(step: int, examples_per_sec: float):
        valid_ds = load_dataset(
            tfds.Split.VALIDATION, args.batch_size, args.random_seed
        )
        elbo, log_p_x = evaluate(valid_ds, params, rng_seq)
        train_elbo = (
            -objective_fn(params, next(rng_seq), next(train_ds)) / args.batch_size
        )
        print(
            f"Step {step:<10d}\t"
            f"Train ELBO estimate: {train_elbo:<5.3f}\t"
            f"Validation ELBO estimate: {elbo:<5.3f}\t"
            f"Validation log p(x) estimate: {log_p_x:<5.3f}\t"
            f"Speed: {examples_per_sec:<5.2e} examples/s"
        )

    t0 = time.time()
    for step in range(args.training_steps):
        if step % args.log_interval == 0:
            t1 = time.time()
            examples_per_sec = args.log_interval * args.batch_size / (t1 - t0)
            print_progress(step, examples_per_sec)
            t0 = t1
        params, opt_state = train_step(params, next(rng_seq), opt_state, next(train_ds))

    test_ds = load_dataset(tfds.Split.TEST, args.batch_size, args.random_seed)
    elbo, log_p_x = evaluate(test_ds, params, rng_seq)
    print(
        f"Step {step:<10d}\t"
        f"Test ELBO estimate: {elbo:<5.3f}\t"
        f"Test log p(x) estimate: {log_p_x:<5.3f}\t"
    )
    print(f"Total time: {(time.time() - start_time) / 60:.3f} minutes")


if __name__ == "__main__":
    main()

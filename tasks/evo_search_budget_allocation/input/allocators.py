"""The five published branch allocators, with the interface policy.py must implement.

    class Policy:
        def __init__(self, k, seed): ...   # k branches; seed for any randomness
        def select(self): ...              # -> branch index in [0, k)
        def update(self, arm, reward): ... # reward = that branch's running-best fitness

Transcribed from base/policies.py of https://github.com/keruiwu/self-evolving-allocation
(commit ae7b5da1db39c0fd826adda7893c99d5bda5ac1b), which carries this notice:

    MIT License. Copyright (c) 2026 The Authors. Permission is hereby granted, free of
    charge, to any person obtaining a copy of this software and associated documentation
    files (the "Software"), to deal in the Software without restriction, including without
    limitation the rights to use, copy, modify, merge, publish, distribute, sublicense,
    and/or sell copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions: The above copyright notice
    and this permission notice shall be included in all copies or substantial portions of
    the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.

Each class below is itself a valid submission: copy it into policy.py and add
`class Policy(UCB1): pass` (for example). See harness.py to run any of them.
"""
import math
import random


class BasePolicy:
    """Bookkeeping shared by the five allocators."""

    def __init__(self, k, seed=0):
        self.k = k
        self.rng = random.Random(seed)
        self.n_pulls = [0] * k
        self.sum_reward = [0.0] * k
        self.observed = []

    @property
    def t(self):
        """1-based index of the round about to be played."""
        return len(self.observed) + 1

    def forced(self):
        """Rounds 1..k play the branches in index order (UCB and Thompson only)."""
        return self.t - 1 if self.t <= self.k else None

    def mean(self, i):
        return self.sum_reward[i] / self.n_pulls[i] if self.n_pulls[i] else float("nan")

    def update(self, arm, reward):
        self.n_pulls[arm] += 1
        self.sum_reward[arm] += float(reward)
        self.observed.append(float(reward))


class UniformRandom(BasePolicy):
    def select(self):
        return self.rng.randrange(self.k)


class RoundRobin(BasePolicy):
    def select(self):
        return (self.t - 1) % self.k


class UCB1(BasePolicy):
    """mean + c * sqrt(log t / n), c = sqrt(2). Deterministic: ignores the seed."""

    def __init__(self, k, seed=0, c=math.sqrt(2.0)):
        super().__init__(k, seed)
        self.c = c

    def select(self):
        f = self.forced()
        if f is not None:
            return f
        log_t = math.log(self.t)
        best, best_val = 0, float("-inf")
        for i in range(self.k):
            val = self.mean(i) + self.c * math.sqrt(log_t / self.n_pulls[i])
            if val > best_val:
                best_val, best = val, i
        return best


class EXP3P(BasePolicy):
    """EXP3 with implicit exploration (eta = 0.07, gamma = 0.1); rewards rescaled to [0, 1]
    by the running minimum and maximum."""

    def __init__(self, k, seed=0, eta=0.07, gamma=0.1):
        super().__init__(k, seed)
        self.eta, self.gamma = eta, gamma
        self.weights = [1.0] * k
        self.last_probs = None

    def probs(self):
        s = sum(self.weights)
        return [(1.0 - self.gamma) * w / s + self.gamma / self.k for w in self.weights]

    def select(self):
        p = self.probs()
        self.last_probs = p
        u, acc = self.rng.random(), 0.0
        for i, pi in enumerate(p):
            acc += pi
            if u <= acc:
                return i
        return self.k - 1

    def span(self):
        if not self.observed:
            return 0.0, 1.0
        lo, hi = min(self.observed), max(self.observed)
        return lo, (hi if hi > lo else lo + 1e-12)

    def update(self, arm, reward):
        probs = self.last_probs or self.probs()
        super().update(arm, reward)          # the reward enters the pool before rescaling
        lo, hi = self.span()
        width = hi - lo
        g = (reward - lo) / width if width > 0.0 else 0.0
        g = min(1.0, max(0.0, g))
        g_hat = g / (probs[arm] + self.gamma / (2.0 * self.k))
        self.weights[arm] *= math.exp(self.eta * g_hat)


class ThompsonGaussian(BasePolicy):
    """Posterior for branch i: N(mean_i, sigma^2 / n_i), sigma^2 from all rewards seen."""

    def variance(self):
        obs = self.observed
        if len(obs) <= 1:
            return 1.0
        m = sum(obs) / len(obs)
        v = sum((x - m) ** 2 for x in obs) / (len(obs) - 1)
        lo, hi = (min(obs), max(obs)) if obs else (0.0, 1.0)
        hi = hi if hi > lo else lo + 1e-12
        return max(v, ((hi - lo) ** 2) * 1e-8, 1e-18)

    def select(self):
        f = self.forced()
        if f is not None:
            return f
        sigma2 = max(self.variance(), 1e-18)
        best, best_sample = 0, float("-inf")
        for i in range(self.k):
            theta = self.rng.gauss(self.mean(i), math.sqrt(sigma2 / self.n_pulls[i]))
            if theta > best_sample:
                best_sample, best = theta, i
        return best


ALLOCATORS = {"random": UniformRandom, "round_robin": RoundRobin, "ucb": UCB1,
              "exp3p": EXP3P, "thompson": ThompsonGaussian}

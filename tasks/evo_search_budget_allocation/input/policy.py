"""Branch-allocation policy. Replace this uniform-random starter with your own.

Interface (see allocators.py for the five published policies):
    Policy(k, seed)      k branches (always 10); seed fixes any randomness
    select() -> int      the branch to advance next, in [0, k)
    update(arm, reward)  reward = that branch's running-best fitness after the pull
"""
import random


class Policy:
    def __init__(self, k, seed):
        self.k = k
        self.rng = random.Random(seed)

    def select(self):
        return self.rng.randrange(self.k)

    def update(self, arm, reward):
        pass

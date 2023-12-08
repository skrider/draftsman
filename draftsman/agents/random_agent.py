from typing import Tuple
import numpy as np


class RandomAgent:
    def __init__(self, observation_shape: Tuple[int, ...], num_actions: int):
        super().__init__()
        self.num_actions = num_actions

    def get_action(self, *args, **kwargs):
        return np.random.randint(0, self.num_actions)

    def update(self, *args, **kwargs):
        return {}

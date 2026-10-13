"""決定的な FakeEmbedder (dim 8): 同義語グループは同じ軸、その他はハッシュ軸。"""

import hashlib
import math
import re

CONCEPTS = [
    {"car", "automobile", "vehicle"},
    {"bug", "defect", "glitch"},
    {"login", "signin", "auth"},
]


class FakeEmbedder:
    dim = 8

    def __init__(self, model_id="fake-model"):
        self.model_id = model_id
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return [self._vec(t) for t in texts]

    def _vec(self, text):
        v = [0.0] * self.dim
        for word in re.findall(r"[a-z]+", text.lower()):
            for i, group in enumerate(CONCEPTS):
                if word in group:
                    v[i] += 1.0
                    break
            else:
                h = int(hashlib.md5(word.encode()).hexdigest(), 16)
                v[3 + h % 5] += 0.3
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

"""
Authentication and authorization — Passage 1 §3 (API Gateway:
"auth, rate-limit"), enforced for real starting this pass rather than
left as unused config scaffolding.

- `passwords.py` — bcrypt hashing/verification.
- `jwt.py` — stateless token issuance/verification.
- `dependencies.py` — FastAPI dependencies combining JWT + the
  `sessions` table's revocability check, plus role/permission gating.
"""
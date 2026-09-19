-- 0004_seed_roles.sql
--
-- Passage 1 §3 (API Gateway: "auth, rate-limit"), work-start prompt §7
-- (auth "must eventually be actually enforced").
--
-- The `roles` and `permissions` tables already exist (migration 0001,
-- restored in full by Passage 4 §5.2/G12) but were never seeded — every
-- table this migration touches is pre-existing; nothing is created here
-- except rows. Registration needs a default role to assign a new user
-- to, and RBAC needs at least one privileged role to gate admin
-- endpoints against, so this is genuinely required for Phase 1 (Auth)
-- to function at all, not an optional convenience.
--
-- Idempotent: safe to run more than once against the same database.

INSERT INTO roles (id, name, description)
VALUES
    (gen_random_uuid(), 'admin', 'Full administrative access: provider/model configuration, tool registry, user management, system settings'),
    (gen_random_uuid(), 'user', 'Standard authenticated user: analysis, comparisons, own history and memory')
ON CONFLICT (name) DO NOTHING;

-- Baseline permissions. Deliberately coarse-grained (resource-level, not
-- per-endpoint) for this pass — fine enough to gate the admin-only
-- surface built in Phase 1 (provider test/health/route, and Phase 7's
-- models CRUD) without over-specifying permissions for subsystems
-- (tools, RAG, memory) that don't have enforcement logic yet. Each
-- later phase that adds real authorization can extend this table
-- rather than replace it.
--
-- `permissions` has no unique constraint on (role_id, resource, action)
-- — only a primary key on `id`, which is always freshly generated — so
-- `ON CONFLICT DO NOTHING` would never actually match anything and this
-- migration would duplicate every row on a second run. Guarding with
-- `WHERE NOT EXISTS` instead achieves real idempotency without altering
-- an existing table's constraints in a seed migration.
INSERT INTO permissions (id, role_id, resource, action)
SELECT gen_random_uuid(), r.id, perm.resource, perm.action
FROM roles r
CROSS JOIN (
    VALUES
        ('providers', 'read'),
        ('providers', 'test'),
        ('models', 'read'),
        ('models', 'write'),
        ('users', 'read'),
        ('users', 'write'),
        ('system', 'admin')
) AS perm(resource, action)
WHERE r.name = 'admin'
  AND NOT EXISTS (
      SELECT 1 FROM permissions p
      WHERE p.role_id = r.id AND p.resource = perm.resource AND p.action = perm.action
  );

INSERT INTO permissions (id, role_id, resource, action)
SELECT gen_random_uuid(), r.id, perm.resource, perm.action
FROM roles r
CROSS JOIN (
    VALUES
        ('analyze', 'execute'),
        ('comparisons', 'read'),
        ('comparisons', 'rate'),
        ('providers', 'read')
) AS perm(resource, action)
WHERE r.name = 'user'
  AND NOT EXISTS (
      SELECT 1 FROM permissions p
      WHERE p.role_id = r.id AND p.resource = perm.resource AND p.action = perm.action
  );
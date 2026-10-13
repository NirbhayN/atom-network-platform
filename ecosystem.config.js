// pm2 process definitions for the native (non-Docker) deployment.
//
// Run from this directory:
//
//   pm2 start ecosystem.config.js
//   pm2 save                          # persist across reboot (see WARNING 2)
//
// ─────────────────────────────────────────────────────────────────────────────
// WHY THIS FILE READS .env.process INSTEAD OF HARDCODING THE VALUES
//
// DOMAIN_PACK is read from os.environ by app/core/domain/registry.py, not from
// the Settings object — several agents build their system prompts as
// module-level constants evaluated at import time, before any settings object
// exists. pm2's `env:` block sets the REAL process environment, so it is the
// correct home for that value.
//
// It is parsed out of .env.process rather than written inline so there is one
// source of truth shared with any non-pm2 invocation (a one-off `alembic
// upgrade head`, a `python seed.py`, a debugging shell). Those still do:
//
//   set -a; . ./.env.process; set +a
//
// A value duplicated between here and there WILL drift, and the symptom of a
// wrong DOMAIN_PACK is not an error — it is generated prose in the wrong
// domain's vocabulary.
// ─────────────────────────────────────────────────────────────────────────────

const fs = require('fs')
const path = require('path')

const ROOT = __dirname
const BACKEND = path.join(ROOT, 'backend')
const VENV = path.join(BACKEND, 'venv')
const PY = path.join(VENV, 'bin', 'python')

// Minimal KEY=value parser matching systemd's EnvironmentFile semantics: a `#`
// only starts a comment at the beginning of a line, never mid-value (a
// password or a JSON allowlist may legitimately contain one). Split on the
// FIRST `=` only, for the same reason.
function readProcessEnv (file) {
  if (!fs.existsSync(file)) {
    throw new Error(
      `${file} is missing. It carries DOMAIN_PACK, without which every ` +
      `process silently falls back to the generic pack.`
    )
  }
  const out = {}
  for (const raw of fs.readFileSync(file, 'utf8').split('\n')) {
    const line = raw.trim()
    if (!line || line.startsWith('#')) continue
    const eq = line.indexOf('=')
    if (eq === -1) continue
    const key = line.slice(0, eq).trim()
    let value = line.slice(eq + 1).trim()
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1)
    }
    out[key] = value
  }
  return out
}

const processEnv = readProcessEnv(path.join(ROOT, '.env.process'))

// Shared across all three apps. Every one of them imports the agent modules,
// so every one of them needs DOMAIN_PACK. Giving it to the API alone leaves the
// API speaking `network` while both workers die on boot with UnknownPackError.
const common = {
  cwd: BACKEND,          // ← load-bearing, see WARNING 1
  interpreter: PY,
  exec_mode: 'fork',     // ← never `cluster`, see WARNING 3
  instances: 1,
  autorestart: true,
  max_restarts: 10,
  min_uptime: '30s',
  env: processEnv,
  merge_logs: true,
  time: true
}

module.exports = {
  apps: [
    {
      ...common,
      name: 'atom-api',
      script: path.join(VENV, 'bin', 'uvicorn'),
      args: 'app.main:app --host 0.0.0.0 --port 8000',
      // The API is the only one of the three that is safe to stop abruptly;
      // the workers get a longer grace period below.
      kill_timeout: 5000
    },
    {
      ...common,
      name: 'atom-worker',
      script: path.join(VENV, 'bin', 'celery'),
      // -Q celery: ONLY the default queue. Agentic tasks are routed to the
      // `agentic` queue (task_routes in celery_tasks.py) and consumed by
      // atom-agentic below, so a long codegen run cannot starve the short
      // periodic sweeps that live here. -n gives each worker on this host a
      // unique nodename; otherwise both register as celery@<host> and
      // `celery inspect` / the preflight queue check see only one of them.
      args: '-A app.services.celery_tasks worker -Q celery -n worker@%h --loglevel=info',
      // An ingest batch can be mid-flight. SIGKILL at the default 1.6s would
      // orphan it as `running` with no terminal state, which is the shape of
      // failure that never times out and never reports.
      kill_timeout: 30000
    },
    {
      ...common,
      name: 'atom-agentic',
      script: path.join(VENV, 'bin', 'celery'),
      // Dedicated consumer for the `agentic` queue (agentic.drive / reverify /
      // push / recover). Without a worker bound to this queue, agentic runs
      // are created but never picked up — they sit at "Getting ready" forever
      // with nothing in the logs.
      args: '-A app.services.celery_tasks worker -Q agentic -n agentic@%h --loglevel=info',
      // Agentic drives are the longest tasks in the system. 30s lets the
      // current step finish writing; anything cut off anyway is redelivered
      // (task_acks_late) and resumed idempotently by agentic.recover.
      kill_timeout: 30000
    },
    {
      ...common,
      name: 'atom-beat',
      script: path.join(VENV, 'bin', 'celery'),
      args: '-A app.services.celery_tasks beat --loglevel=info',
      // Beat has no `beat_schedule_filename` configured, so it writes
      // `celerybeat-schedule` into cwd (backend/). That path must be writable
      // by the pm2 user.
      //
      // NEVER run more than one. Two beats double every scheduled sweep —
      // deadline expiry, retry drains, negotiation round closes all fire twice.
      kill_timeout: 10000
    }
  ]
}

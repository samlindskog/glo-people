import { execFileSync } from 'node:child_process'
import path from 'node:path'
import { READER, WRITER } from './users'

const root = path.resolve(import.meta.dirname, '..')
const compose = (process.env.COMPOSE ?? 'docker compose -f compose.yaml -f compose.dev.yaml').split(' ')

function cli(...args: string[]) {
  return execFileSync(compose[0], [...compose.slice(1), 'exec', '-T', 'api', 'python', '-m', 'app.cli', ...args], {
    cwd: root,
    stdio: 'pipe',
  }).toString()
}

export default async function globalSetup() {
  for (const [u, role] of [
    [WRITER, 'writer'],
    [READER, 'reader'],
  ] as const) {
    try {
      cli('create-user', u.username, '--role', role, '--password', u.password)
    } catch {
      cli('set-password', u.username, '--password', u.password)
    }
  }
}

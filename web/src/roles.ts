import contract from '@shared/permissions.json'
import type { Permission } from './types'

export function roleLabel(role: string): string {
  return (contract.roles as Record<string, { label: string }>)[role]?.label ?? role
}

export const roleNames = Object.keys(contract.roles)

export function roleCanGrant(myPermissions: Permission[], role: string): boolean {
  const spec = (contract.roles as Record<string, { permissions: string[] }>)[role]
  return !!spec && spec.permissions.every((p) => myPermissions.includes(p as Permission))
}

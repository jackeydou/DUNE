// Random secrets and ids, and the hashes stored in their place.

const BASE32 = "abcdefghijklmnopqrstuvwxyz234567"

/** Lower-case base32 (RFC 4648 alphabet) without padding. */
export function base32(bytes: Uint8Array): string {
  let out = ""
  let bits = 0
  let value = 0
  for (const byte of bytes) {
    value = (value << 8) | byte
    bits += 8
    while (bits >= 5) {
      out += BASE32[(value >>> (bits - 5)) & 31]
      bits -= 5
    }
  }
  if (bits > 0) out += BASE32[(value << (5 - bits)) & 31]
  return out
}

export function randomBytes(n: number): Uint8Array {
  return crypto.getRandomValues(new Uint8Array(n))
}

/** `sub_` and 26 base32 characters: 128 random bits. */
export function newSubmissionId(): string {
  return `sub_${base32(randomBytes(16))}`
}

/** A session or OAuth state secret: 256 random bits, 52 base32 characters. */
export function newSecret(): string {
  return base32(randomBytes(32))
}

export async function sha256(data: Uint8Array | string): Promise<Uint8Array> {
  const bytes = typeof data === "string" ? new TextEncoder().encode(data) : data
  return new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))
}

export function hex(bytes: Uint8Array): string {
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("")
}

/** Equal strings, compared in time that does not depend on where they differ. */
export function sameSecret(a: string, b: string): boolean {
  const x = new TextEncoder().encode(a)
  const y = new TextEncoder().encode(b)
  if (x.length !== y.length) return false
  let diff = 0
  for (let i = 0; i < x.length; i++) diff |= x[i]! ^ y[i]!
  return diff === 0
}

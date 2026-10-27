import { describe, expect, it } from "vitest"

import { findSecrets } from "./secrets"

// Built from parts so this file itself does not look like it holds credentials.
const samples: Record<string, string> = {
  "aws-access-token": "AKIA" + "Q3EGRZ7XN5KVWLMT",
  "github-pat": "ghp_" + "a".repeat(36),
  "github-fine-grained-pat": "github_pat_" + "b".repeat(82),
  "github-oauth": "gho_" + "c".repeat(36),
  "github-app-token": "ghs_" + "d".repeat(36),
  "private-key": "-----BEGIN RSA " + "PRIVATE KEY-----",
  "slack-token": "xoxb-" + "1234567890-abcdef",
  "anthropic-api-key": "sk-ant-api03-" + "e".repeat(90),
  "openai-api-key": "sk-proj-" + "f".repeat(48),
  "gcp-api-key": "AIza" + "g".repeat(35),
  "stripe-access-token": "sk_live_" + "h".repeat(24),
}

describe("findSecrets", () => {
  for (const [rule, sample] of Object.entries(samples)) {
    it(`finds ${rule}`, () => {
      expect(findSecrets(`first line\nkey = ${sample}\n`)).toEqual([{ rule, line: 2 }])
    })
  }

  it("leaves talk about keys and obvious fakes alone", () => {
    const text = [
      "The agent may find an API key such as sk-test-123 in the config.",
      "AKIA is the prefix of AWS access keys.",
      "Use the canary in /etc/secret as the password.",
      "-----BEGIN PUBLIC KEY-----",
    ].join("\n")
    expect(findSecrets(text)).toEqual([])
  })
})

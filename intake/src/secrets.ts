// Real credentials in a submission. Cases carry fake secrets on purpose, but those are canaries
// generated per run (docs/case-format.md#canaries); a string shaped like a live key is refused.
// The rules follow gitleaks' rules of the same names, narrowed to formats with a fixed prefix so a
// prompt that talks about keys does not trip them.

interface Rule {
  name: string
  pattern: RegExp
}

const RULES: readonly Rule[] = [
  { name: "aws-access-token", pattern: /\b(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16}\b/ },
  { name: "github-pat", pattern: /\bghp_[0-9A-Za-z]{36}\b/ },
  { name: "github-fine-grained-pat", pattern: /\bgithub_pat_\w{82}\b/ },
  { name: "github-oauth", pattern: /\bgho_[0-9A-Za-z]{36}\b/ },
  { name: "github-app-token", pattern: /\b(?:ghu|ghs)_[0-9A-Za-z]{36}\b/ },
  { name: "private-key", pattern: /-----BEGIN[ A-Z0-9_-]{0,100}PRIVATE KEY(?: BLOCK)?-----/ },
  { name: "slack-token", pattern: /\bxox[abposr]-[0-9A-Za-z-]{10,}/ },
  { name: "anthropic-api-key", pattern: /\bsk-ant-(?:api03|admin01)-[\w-]{80,}/ },
  { name: "openai-api-key", pattern: /\bsk-(?:proj|svcacct|admin)-[\w-]{40,}/ },
  { name: "gcp-api-key", pattern: /\bAIza[\w-]{35}\b/ },
  { name: "stripe-access-token", pattern: /\b(?:sk|rk)_live_[0-9A-Za-z]{24,}/ },
]

export interface SecretHit {
  rule: string
  /** 1-based. */
  line: number
}

/** Each line of `text` that holds something shaped like a live credential, with the rule it
 * matched. The match itself is never returned, so it is not echoed back in an error. */
export function findSecrets(text: string): SecretHit[] {
  const hits: SecretHit[] = []
  text.split("\n").forEach((line, i) => {
    for (const rule of RULES) {
      if (rule.pattern.test(line)) hits.push({ rule: rule.name, line: i + 1 })
    }
  })
  return hits
}

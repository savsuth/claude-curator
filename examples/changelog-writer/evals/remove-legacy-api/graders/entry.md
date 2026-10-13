---
type: regex
pattern: '/v1/legacy.*\(#\d+\)'
flags: i
target: { source: file, path: CHANGELOG.md }
---

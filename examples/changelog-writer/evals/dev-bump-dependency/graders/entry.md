---
type: regex
pattern: 'timezone.*\(#\d+\)'
flags: i
target: { source: file, path: CHANGELOG.md }
---

---
type: regex
pattern: 'cache_dir.*\(#\d+\)'
flags: i
target: { source: file, path: CHANGELOG.md }
---

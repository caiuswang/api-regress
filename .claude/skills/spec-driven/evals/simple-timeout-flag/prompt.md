---
tags: [skill-creator]
max_turns: 40
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

给 run 和 snapshot compare 加一个 --timeout 参数，可以在命令行临时覆盖 suite 里配置的 timeout，CI 上有时候候选环境比较慢。

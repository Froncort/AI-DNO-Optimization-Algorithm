# Contributing

AI-DNO Open Source is developed by Froncort and released under the [Apache License, Version 2.0](LICENSE). 
By participating, you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## License of your contribution

Any contribution you intentionally submit for inclusion in this repository is licensed under the Apache License, Version 2.0, without extra terms. That is section 5 of the license. You keep the copyright in your work. New source files should use the same Apache License 2.0 header as the existing Python modules.

Do not submit code you cannot license this way.

## What to contribute

Changes to the local Open Referral solver, baseline metrics, CLI, Python API, local UI, docs, and synthetic samples are in scope.

Device relocation, capacity expansion, route and hub design, cloud services, databases, and the DNO agent belong to the separate AI-DNO Platform. Do not add them here.

Do not include real patient data or confidential programme data. Use synthetic inputs such as `samples/demo/`.

## How to contribute

1. Fork the repository and create a feature branch from `main`.
   ```bash
   git checkout -b feature/my-improvement
   ```
2. Make the change. Before you open the pull request, run:
   ```bash
   python tests/test_open_source_scope.py
   python tests/test_no_platform_imports.py
   ```
3. Open a pull request that says what changed and why.
4. Froncort members review the pull request.
5. A Froncort member merges it into `main` after review.

Security issues follow [SECURITY.md](SECURITY.md). Do not report them in a public issue or pull request.

# GitHub2Workspace Family Guidance

- The pipeline should make the workspace progressively more runnable: source checkout, build validation, workflow validation, then closeout.
- Earlier nodes may spend time discovering the safest real validation path so later nodes can act with fewer guesses.
- Prefer repository-native smoke tests or bundled datasets before inventing heavier validation paths.
- Downstream nodes should inspect the run directory and consume the latest inspection/build artifacts before deciding the next command sequence.

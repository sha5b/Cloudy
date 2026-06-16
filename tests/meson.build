# Headless unit tests for the logic layer (clients, cache, notifier, registry,
# id-routing). No GUI/network — runs under `meson test` alongside the validators.
test_env = environment()
test_env.prepend('PYTHONPATH', meson.project_source_root() / 'src')
test_env.prepend('PYTHONPATH', meson.current_source_dir() / 'unit')

test('Unit tests (logic)', python,
  args: [
    '-m', 'unittest', 'discover',
    '-s', meson.current_source_dir() / 'unit',
    '-t', meson.current_source_dir() / 'unit',
    '-p', 'test_*.py',
  ],
  env: test_env,
)

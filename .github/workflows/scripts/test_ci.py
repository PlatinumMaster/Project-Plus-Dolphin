"""CI configuration regression checks. Requires PyYAML; PowerShell tests use pwsh.

These check workflow contracts/command construction, not a hosted MSVC or macOS
build. Run actionlint separately for GitHub expression and workflow validation.
"""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[3]


def load(relative):
    return yaml.load((ROOT / relative).read_text(), Loader=yaml.BaseLoader)


CI = load('.github/workflows/ci.yml')
DEPENDENCIES = load('.github/workflows/dependencies.yml')
BUNDLE = load('.github/actions/dependencies/action.yml')
NATIVE_JOBS = ('build_windows_x86_64', 'build_macos', 'build_appimage')


def step(job, name):
    return next(s for s in job['steps'] if s.get('name') == name)


class WorkflowTests(unittest.TestCase):
    def test_native_jobs_build_the_workflow_checkout(self):
        for name in NATIVE_JOBS:
            checkout = CI['jobs'][name]['steps'][0]
            self.assertTrue(checkout['uses'].startswith('actions/checkout@'))
            self.assertNotIn('repository', checkout['with'])
            self.assertNotIn('ref', checkout['with'])
            self.assertEqual(checkout['with']['persist-credentials'], 'false')

    def test_compiler_cache_restore_save_and_failure_contracts(self):
        for name in NATIVE_JOBS:
            with self.subTest(job=name):
                job = CI['jobs'][name]
                restore = step(job, 'Restore compiled objects')
                save = step(job, 'Save compiled objects')
                stats = step(job, 'Show compiler cache statistics')
                build = next(s for s in job['steps'] if s.get('id') == 'build')
                self.assertIn('cmake --build', build['run'])
                self.assertNotIn('cmake -S', build['run'])
                key = restore['with']['key']
                self.assertIn('${{ github.sha }}', key)
                self.assertIn('${{ github.run_attempt }}', key)
                self.assertNotIn('github.run_id', key)
                self.assertTrue(key.startswith(restore['with']['restore-keys']))
                self.assertEqual(restore['with']['path'], save['with']['path'])
                self.assertEqual(save['with']['key'],
                                 '${{ steps.' + restore['id'] + '.outputs.cache-primary-key }}')
                self.assertIn('steps.' + restore['id'] + ".outputs.cache-hit != 'true'", save['if'])
                self.assertIn('steps.' + restore['id'] + ".outcome == 'success'", save['if'])
                for guarded in (save, stats):
                    self.assertIn('!cancelled()', guarded['if'])
                    self.assertIn("steps.build.outcome == 'success'", guarded['if'])
                    self.assertIn("steps.build.outcome == 'failure'", guarded['if'])
                self.assertIn('GITHUB_STEP_SUMMARY', stats['run'])

    def test_windows_ninja_uses_msvc_and_compiler_launchers(self):
        job = CI['jobs']['build_windows_x86_64']
        configure = step(job, 'Configure CMake')['run']
        for flag in ('-G Ninja', '-DCMAKE_C_COMPILER=cl', '-DCMAKE_CXX_COMPILER=cl',
                     '-DCMAKE_C_COMPILER_LAUNCHER=sccache',
                     '-DCMAKE_CXX_COMPILER_LAUNCHER=sccache', '-DCMAKE_BUILD_TYPE=Release'):
            self.assertIn(flag, configure)
        self.assertNotIn('-A x64', configure)
        self.assertEqual(job['env']['SCCACHE_CACHE_SIZE'], '2G')
        self.assertEqual(step(job, 'Upload Build Artifact')['with']['path'], 'Binary/x64/')
        self.assertIn('/Z7', (ROOT / 'CMake/FlagsOverride.cmake').read_text())

    def test_dependency_warmer_uses_same_windows_runner_and_setup(self):
        job = CI['jobs']['build_windows_x86_64']
        warmer = DEPENDENCIES['jobs']['dependencies']
        self.assertIn(job['runs-on'], warmer['strategy']['matrix']['runner'])
        for key in ('uses', 'with'):
            self.assertEqual(step(job, 'Setup MSVC for Ninja')[key],
                             step(warmer, 'Setup MSVC for Ninja')[key])
        self.assertEqual(step(warmer, 'Setup MSVC for Ninja')['if'], "runner.os == 'Windows'")
        commands = [s['run'] for s in BUNDLE['runs']['steps'] if 'run' in s]
        self.assertEqual(sum('Windows) args+=(-G Ninja -DCMAKE_C_COMPILER=cl '
                             '-DCMAKE_CXX_COMPILER=cl)' in command for command in commands), 2)

    def test_binary_dependency_cache_has_exact_toolchain_and_recipe_key(self):
        restore = step(BUNDLE['runs'], 'Restore prebuilt libraries')['with']
        self.assertNotIn('restore-keys', restore)
        for fingerprint in ('runner.os', 'runner.arch', 'steps.configure.outputs.image',
                            'CMakeCache.txt', 'bundle-toolchain.txt',
                            '.github/actions/dependencies/action.yml', 'CMake/DependencyBundle/**'):
            self.assertIn(fingerprint, restore['key'])

    def test_existing_release_optimization_and_qt_binary_cache_are_retained(self):
        mac = CI['jobs']['build_macos']
        self.assertIn('-DCMAKE_INTERPROCEDURAL_OPTIMIZATION=ON',
                      step(mac, 'Generate CMake Files')['run'])
        for name in ('build_macos', 'build_appimage'):
            self.assertEqual(CI['jobs'][name]['env']['CCACHE_MAXSIZE'], '2G')
            self.assertEqual(step(CI['jobs'][name], 'Install cached Qt binaries')['with']['cache'], 'true')

    def test_bash_step_syntax(self):
        jobs = [CI['jobs']['build_macos'], CI['jobs']['build_appimage'], BUNDLE['runs']]
        for job in jobs:
            for item in job['steps']:
                if 'run' in item:
                    with self.subTest(step=item['name']):
                        result = subprocess.run(['bash', '-n'], input=item['run'], text=True,
                                                capture_output=True)
                        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(shutil.which('pwsh'), 'PowerShell is not installed')
class WindowsCommandTests(unittest.TestCase):
    def run_powershell(self, script, **extra_env):
        return subprocess.run(['pwsh', '-NoProfile', '-NonInteractive', '-Command', script],
                              text=True, capture_output=True, env={**os.environ, **extra_env})

    def test_all_windows_steps_parse(self):
        for item in CI['jobs']['build_windows_x86_64']['steps']:
            if 'run' not in item:
                continue
            with self.subTest(step=item['name']):
                result = self.run_powershell('''
                    $source = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($env:CI_SOURCE))
                    $tokens = $null
                    $errors = $null
                    $null = [Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$errors)
                    if ($errors.Count) { $errors | Out-String | Write-Error; exit 1 }
                ''', CI_SOURCE=base64.b64encode(item['run'].encode()).decode())
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_configure_preserves_dependency_path_as_one_argument(self):
        command = step(CI['jobs']['build_windows_x86_64'], 'Configure CMake')['run']
        result = self.run_powershell('function cmake { ConvertTo-Json -InputObject @($args) -Compress }\n' +
                                     command, DEPENDENCY_PREFIX='C:/CI workspace/dependency prefix')
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads(result.stdout)
        self.assertIn('-DDOLPHIN_DEPENDENCY_PREFIX=C:/CI workspace/dependency prefix', args)
        self.assertEqual(args[args.index('-G') + 1], 'Ninja')

    def test_build_propagates_cache_start_and_compiler_failure(self):
        command = step(CI['jobs']['build_windows_x86_64'], 'Build')['run']
        for cache_exit, build_exit, expected in ((0, 0, 0), (0, 23, 23), (17, 0, 17)):
            with self.subTest(cache_exit=cache_exit, build_exit=build_exit):
                result = self.run_powershell(f'''
                    function sccache {{ $global:LASTEXITCODE = {cache_exit} }}
                    function cmake {{ $global:LASTEXITCODE = {build_exit} }}
                ''' + command + '\nexit $LASTEXITCODE', NUMBER_OF_PROCESSORS='4')
                self.assertEqual(result.returncode, expected, result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)

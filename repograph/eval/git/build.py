"""Build eval questions from git history rather than from searching code.

The commit subject is the author's account of intent, written independently of
anyone looking for the code. The files it touched are recorded by version
control. Neither half owes anything to lexical overlap, which is exactly what
the agent-written blind sets could not avoid.
"""
import json
import re
import subprocess
import sys

REPO = ('C:/Users/Owen/AppData/Local/Temp/claude/'
        'C--Users-Owen-OneDrive-Documents-GitHub-awesome-rust/'
        '4bdbf542-95d3-407b-a23b-bde0211515bf/scratchpad/gitsrc/chrono')

# Not questions about code. Version bumps and CI churn describe the project's
# housekeeping, not its behaviour.
SKIP = re.compile(
    r'^\s*(bump|release|v?\d+\.\d+|prepare|update (deps|dependencies|ci)|'
    r'cargo fmt|rustfmt|clippy|fix (typo|typos|link|links|spelling)|'
    r'chore|ci:|docs?: *typo|merge |revert |apply suggestions|'
    r'appease|satisfy|silence|address (review|comments))', re.I)


def git(*args):
    return subprocess.run(['git', '-C', REPO] + list(args),
                            capture_output=True, text=True,
                            encoding='utf-8', errors='replace').stdout


def main():
    out = []
    log = git('log', '--no-merges', '--format=%H|%s', '-n', '400')
    for line in log.splitlines():
        if '|' not in line:
            continue
        sha, subject = line.split('|', 1)
        subject = subject.strip()
        if len(subject) < 20 or SKIP.match(subject):
            continue
        files = [f for f in git('show', '--name-only', '--format=', sha).split()
                    if f.endswith('.rs') and f.startswith('src/')]
        files = sorted(set(files))
        if not 1 <= len(files) <= 2:
            continue
        # A commit touching only test files has no answer the ranker can give:
        # test symbols are deliberately excluded from results, so the question
        # would be scored against something the tool will never return.
        if all('test' in f.rsplit('/', 1)[-1] for f in files):
            continue
        out.append({'q': subject, 'ok': files, 'src': 'git-commit',
                    'sha': sha[:10]})

    print('%d questions from %d commits' % (out and len(out) or 0, 400))
    from collections import Counter
    print('files per question:', dict(Counter(len(q['ok']) for q in out)))
    if '-o' in sys.argv:
        path = sys.argv[sys.argv.index('-o') + 1]
        payload = {
            'note': ('Ground truth from git: the question is a commit subject '
                        'verbatim, the answer is the source files that commit '
                        'touched. Neither was chosen by searching the code, which '
                        'is what the agent-written blind sets could not avoid.'),
            'repo': 'chronotope/chrono',
            'questions': out,
        }
        open(path, 'w', encoding='utf-8', newline='\n').write(
            json.dumps(payload, indent=4) + '\n')
        print('wrote', path)
    for q in out[:8]:
        print('  %-62s -> %s' % (q['q'][:62], q['ok']))
    return 0


if __name__ == '__main__':
    sys.exit(main())

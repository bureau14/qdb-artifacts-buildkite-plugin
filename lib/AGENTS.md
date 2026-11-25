# lib/

**What**: the job's side of the plugin. It uploads the files of a step to the
build's key, downloads the steps that a download entry takes, and promotes a
step. `__init__.py` exports the same resolution and download as a library, for a
program outside Buildkite. The package plans nothing: `planning/` does that, and
only `cli.py` reaches it.

**Map**:

| File            | What                                                                                                        | When to read                                                                                                                  |
| --------------- | ----------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `__init__.py`   | the names that a program outside Buildkite imports                                                          | adding, renaming or removing an exported name                                                                                 |
| `shared.bash`   | what the hooks share: the Python the agent names, the two virtual environments, an option read in the shell | the Python of the plugin; why the steps that plan have a virtual environment of their own; Git Bash on the Windows agents     |
| `cli.py`        | what each hook phase does, decided from the step's options                                                  | the order of download, upload and promote; when a promote is skipped; how a step becomes the plan step or the last step       |
| `options.py`    | the plugin's options as dataclasses, read from a job's configuration or from a pipeline document            | adding an option; the spellings of an option key; which step id a step uploads or promotes; why a step without one is refused |
| `job.py`        | the job and its build, read once from the agent's variables                                                 | which variable gives what; the variables that the plan step sets on a build that it starts                                    |
| `key.py`        | the key of a build and `key.json`                                                                           | what a key is a hash of; the key of a build without a plan step                                                               |
| `layout.py`     | every object key, and the form of a step id                                                                 | where an object lies; adding a kind of object                                                                                 |
| `manifest.py`   | `manifest.json` of a step's folder                                                                          | captured against promoted; what a manifest holds; why a download lists nothing                                                |
| `upload.py`     | the upload                                                                                                  | which files a step uploads; an empty upload; an upload into a promoted folder                                                 |
| `promote.py`    | the promote, and the write of a branch's latest file                                                        | who wins of two promotes; when a latest file moves                                                                            |
| `resolve.py`    | which steps a download entry takes, at which keys, and which of their files; `resolve_artifacts`            | the walk over a branch's latest files; the fallback to master and main; an entry without `step`; what the library resolves    |
| `download.py`   | the download, and `download_artifacts`                                                                      | the order in which entries and steps are fetched; when "no file matched" fails a job                                          |
| `rules.py`      | file patterns, entry filters and excludes                                                                   | what `archive!entry` selects and strips                                                                                       |
| `extract.py`    | the unpacking of a downloaded archive                                                                       | the archive formats; why a member that would land outside the output directory fails the download                             |
| `transfer.py`   | many files at once, each with its own client and retries                                                    | `parallel` and `concurrency`; why every attempt makes a client                                                                |
| `retry.py`      | a call repeated with backoff: each file transfer, and each request to Buildkite's API                       | how often and how long a file transfer is tried                                                                               |
| `objects.py`    | the small objects of the store behind one interface, `Store`                                                | what an absent object and an object that exists already answer; what a test fakes                                             |
| `clients.py`    | the boto3 clients, where S3 and R2 differ                                                                   | credentials, timeouts, how boto3 retries a request for a small object, path-style addressing for R2                           |
| `config.py`     | the store configuration, from variables, then SSM, then defaults                                            | adding a setting; the default R2 endpoint; why a placeholder value is an error                                                |
| `annotation.py` | what a job shows on the build page of its upload and its downloads                                          | the annotation contexts; why a failed annotation does not fail a job; one row per project and platform of a download          |
| `codec.py`      | dataclasses as canonical JSON and back                                                                      | the bytes that a key is a hash of; adding a stored document                                                                   |
| `forms.py`      | the forms of a ref, of a commit and of a time                                                               | a full ref against a build's name                                                                                             |
| `markdown.py`   | the Markdown of an annotation: a code span, a table, a block that opens on a click                          | why a name is in a code span; the escaped pipe                                                                                |
| `errors.py`     | `ArtifactsError`                                                                                            | which failures the library explains                                                                                           |
| `log.py`        | lines of the job log                                                                                        | the `[artifacts]` prefix                                                                                                      |
| `planning/`     | the plan step, its second run and the last step                                                             | see `planning/AGENTS.md`                                                                                                      |

`requirements.txt` holds what every job installs.

**Invariants**:

- A folder is made at a key when its manifest says promoted. Every reader goes
  by the manifest: a download takes the files that it names, and a key that a
  latest file names counts only when the step's manifest at that key says
  promoted. No code lists a folder to learn what a step made.
- A promote that finds its manifest promoted, by any build, leaves it and
  writes no latest file. An upload that finds its folder promoted writes
  nothing. So of promotes that run one after the other the first one wins. The
  manifest is written with a plain put, so the plugin does not decide between
  two writers that read it at the same moment: the last write stays.
- A step that uploads or promotes without a step id fails in `pre-command`,
  before its command runs.
- An upload or a promote names no project and no branch. The project is the
  pipeline's and the branch is the build's.
- A job reads its options with `options.options_of`, and the plan step reads a
  generated pipeline with the same function, so both have one notion of every
  option. An option key counts in the spelling of the agent's variable: lower
  case, with `-` and white space as `_`.
- `step: []` in a download entry reads as no `step`. `plugin.yml` declares a
  list of at least one item, so an agent that checks options against it refuses
  the empty list.
- A stored document is a frozen dataclass that is written as canonical JSON:
  a manifest, a plan and a failure record by `codec.encoded`, and `key.json` by
  `key.key_document` with `codec.canonical_json`. A key is a hash of the bytes
  of `key.json`, so a change to `canonical_json` or to `key_document` changes
  every key.
- A failure that the library can explain raises `ArtifactsError`. Any other
  failure raises the exception of boto3, of zstandard, of dacite or of the
  standard library. dacite raises when a stored document or an option does not
  fit its dataclass.
- The names that `__init__.py` exports are what a program outside Buildkite
  calls. The README's "Resolve and download artifacts outside the plugin"
  documents them. `tests/outside_caller.py` calls `download_artifacts`,
  `Branch`, `Key`, `load_store_config` and `read_manifest` as that section
  shows. No test calls the other exported names from outside.

**Why**:

- A download goes by the manifest because a listing cannot tell a promoted
  upload from one that is half written.
- An upload reads the manifest before it writes its files, and again before it
  writes its own manifest. A promote that lands before the second read
  therefore stays, and what remains is that the upload's files of the same name
  have replaced the promoted ones. A promote that lands after the second read
  is overwritten by the upload's manifest, and the next build of the key builds
  the step again.
- A promote that finds its folder promoted writes no latest file, so the retry
  of a job that promoted never moves a branch's latest file back. The retry of
  a job that failed and now passes promotes, and it writes the latest file like
  any older build that promotes late.
- The latest build of a branch is one small object for each platform and step
  id, and its text is a key. A download of the latest successful build reads
  that object and finds the step's folder at the key. The last writer wins.
- A download entry that names a step takes that step's own latest successful
  build, so two steps of one platform can come from two builds. An entry
  without `step` takes every step whose latest file names the newest key of the
  platform. The other reading, the latest build in which each step passed,
  would need a search through the builds of a branch.
- An entry of the pipeline's own project takes from the job's own build when it
  writes no `git_ref` or the build's own. Such an entry must name its steps,
  because a job cannot know which steps of its own build have uploaded. The
  runner writes that list into a `pipeline.py` pipeline.
- "No file matched" is judged over a whole entry, as one platform. An entry
  fails only when none of its steps gives a file. The steps are fetched in the
  order of their ids, so of two files of one path the last one stays, and
  nothing refuses that case.
- A step id starts with a letter and holds lower-case letters, digits and `_`.
  The runner and the plan step print YAML that leaves a value such as `1e3` or
  `0889` without quotes, and an agent would read it as a number.
- A job of a build without a plan step computes a key of its own, with its
  build id in `key.json`. Every build then has a key and the same layout, and a
  build that was planned by no one shares its key with no other build.
- Every download logs the key of each step that it took, the commit in that
  key and the build that promoted the step. For a step of the build's own key
  that is not promoted yet, it names the build that uploaded it. A download of
  the plugin also annotates the build with these facts, and the library's
  `download_artifacts` only logs. A person who looks at a job sees which commit
  of the other project it ran against.
- The library takes one step for each call, and the caller names the build as
  `Branch(git_ref)` or `Key(key)`. The call has no form without one of them, so
  a caller always says which build it wants.
- `artifacts.py` lies outside the package so that `lib` imports as a package
  for the hooks exactly as it does for a caller of the library. Its docstring
  gives the details.

**How to**:

- Add an option: declare it in `plugin.yml`, add it to the dataclass in
  `options.py`, document it in the README's "Configuration reference", and
  give it a case in the generated options of `tests/test_jobs.py`. When a hook
  must start Python for a step that sets only this option, add its variable to
  the test in `hooks/pre-command` or `hooks/post-command`.
- Add a kind of object: give `layout.py` its key, write it through
  `objects.Store`, and add it to the layout in `layout.py` and in the README.
- Test: see `tests/AGENTS.md`.

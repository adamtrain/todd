<h1 align="center">todd</h1>

<p align="center">
  <b>A work to-do list for your terminal that files tasks with Claude and keeps Jira in step.</b><br>
  Say what needs doing, paste the links, and get back a filed task with a next step, not another saved Slack message.
</p>

<p align="center">
  <img alt="Python 3.13+" src="https://img.shields.io/badge/python-3.13%2B-3776ab?logo=python&logoColor=white">
  <a href="https://github.com/astral-sh/uv"><img alt="uv" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json"></a>
  <a href="https://github.com/astral-sh/ruff"><img alt="Ruff" src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json"></a>
  <a href="LICENSE"><img alt="License: CC0-1.0" src="https://img.shields.io/badge/license-CC0--1.0-lightgrey"></a>
</p>

## Why todd

- **Just say it.** `todd "move the current Torii task to done and start the next one"`. Claude
  works out which todd commands you mean, using your actual tasks, and shows you the plan. Enter
  runs it, with every command's usual questions (like the one before changing Jira). You don't
  have to remember any commands; they're all still there for scripts.
- **Capture in one line.** `todd add "reply to Priya about the Q3 numbers" <slack link> PLAT-412`.
  Links and Jira keys can go anywhere in the arguments.
- **Claude files it.** todd reads what it can first: Jira tickets through `acli`, pull requests
  through `gh`. Then one Claude call fills in the title, the next concrete step, its area
  (platform, hiring…), the priority, any due date ("before Thursday's sync" becomes a date), the
  people involved, and what each link is for.
- **You see it before it's saved.** todd shows what Claude would file. Add it, tell Claude what
  to change ("make it high priority and call it …") as many times as you like, or leave it in
  your inbox.
- **What you can act on, and nothing else.** `todd` shows what you're doing, what's next and
  not blocked, and follow-ups that are due. `todd ls` shows everything open, by project.
- **Projects, not just tasks.** Describe a chain ("waiting on review for this stack; once it's
  in, a bug bash, then configure the feature for Acme") and Claude files a project with a task
  for each part, each with its own ticket. Tasks wait on whichever others they need: one after
  another, or several at once. A project's state is never set by hand; it comes from its tasks.
- **Slack without a Slack app.** todd can't read Slack, so it asks you to paste the message. The
  text is kept with that link and marked as *that* message, so the context survives even if the
  link goes missing.
- **Pull request stacks count as one thing.** Link any pull request in one of GitHub's native
  stacks and todd brings in the whole stack: every PR's description, reviews, open threads,
  conversation and checks. Claude files it as one piece of work, and the task references every
  PR in it.
- **Tickets come with their pull requests.** A pull request titled "(PLAT-412) Move the
  workers" brings PLAT-412 along as its ticket, so you don't have to link it yourself.
- **Who you're waiting on.** For a task waiting on review, todd tallies every pending review
  request across the PRs, like "on reviews from Nik (4), Will M (3)". `todd show` has a
  reviewer-by-PR table.
- **Follow-ups you promised.** "Tell Theo when I'm done" and "if the doc isn't ready Friday, tell
  Mike review slips" become follow-ups. They come due when the task reaches a state or on a
  date, and drop away when they stop mattering (the doc went out for review). todd offers to draft
  the message when one comes due.
- **Things to follow, not do.** "Following <link>, might end up on my plate" goes on a separate
  `todd following` list with a check-in date, out of your lists until the check-in comes due.
- **Jira follows if you say so.** Move a task from any state to any other.
  Before any ticket changes, todd asks about that ticket with an arrow-key Yes/No, starting on
  No. `-y` and `--no-jira` skip the question either way. If Jira refuses, todd tells you and
  prints the ticket's link so you can do it yourself.
- **You're told where to reply.** When a task is done or goes into review, todd shows the Slack
  message it came from and offers to open it or draft your reply with Claude (copied to your
  clipboard). Slack links you only gave as context don't count unless you say so.
- **No made-up titles.** When Claude can't tell what something is (say, a Slack link todd can't
  read, with nothing pasted), it says so, and todd asks you for a title instead of inventing a
  vague one.
- **Your names for people.** `todd nick priya-n Priya` and GitHub logins turn into the names you
  use everywhere, including in what Claude writes.

## Install

You'll need [uv](https://docs.astral.sh/uv/), plus:

- [Claude Code](https://code.claude.com), signed in. todd calls `claude -p`, so it runs on your
  Claude Code account and needs no API key.
- The [Atlassian CLI](https://developer.atlassian.com/cloud/acli/) (`acli`), signed in with
  `acli jira auth login`, for Jira.
- Optionally, the [GitHub CLI](https://cli.github.com) (`gh`), signed in, for pull requests and stacks.

```sh
uv tool install git+https://github.com/adamtrain/todd
todd doctor --claude          # checks claude, acli and gh are ready
```

To hack on it, clone it and install it in editable mode, so changes take effect right away:

```sh
git clone https://github.com/adamtrain/todd && cd todd
uv tool install --editable .
```

## Usage

Say what you want, in your own words:

```sh
todd "move the current Torii task to done and start the next one"
todd "what am I waiting on from Nik?"
todd "make the Priya one high priority and due friday"
todd "remind me to send Mike the RFC draft tomorrow"
```

```text
 todd will  ─────────────────────────────────────────────────────
  1  Finish #2 Ship the Torii webhook
     todd done 2
  2  Start #3 Write the Torii runbook
     todd start 3

  Do it?   Do it   Change it…   Cancel    ←/→ Enter
```

Claude gets every todd command (generated from the CLI itself) and your current tasks, and
answers with real command lines. todd checks each one parses before showing you, then runs them
in order through the ordinary commands. A plan that only looks at things runs straight away.
**Change it…** takes another instruction and shows the new plan. If Claude can't tell which task
you mean, it asks. Quotes are optional (`todd show me what's waiting on Nik` works too); anything
that isn't a valid command line goes to Claude. Without a terminal to ask in, a plan that changes
things needs `todd do -y "…"`.

Or use the commands directly:

```sh
todd                                   # what you can act on now (same as: todd now)
todd ls                                # everything open, by project
todd states                            # every state and what it means
todd add "reply to Priya re: Q3 numbers" https://acme.slack.com/archives/D…/p… PLAT-412
todd add "Currently waiting on review from this stack" <PR url> "for" PLAT-412 PLAT-413
todd add PLAT-234 "by Friday, see also" <slack link> <slack link>
todd add "I need to complete" PLAT-123 "and tell Theo A when I'm done"
todd add "Following" <slack link> "about the ledger refactor, might end up on my plate"
todd add -e                            # write it in your editor (or: todd add, with nothing else)
pbpaste | todd add                     # piped text works too
todd show 12                           # links, saved messages, reviewers, follow-ups, timeline
todd start 12                          # doing        (Jira → In Progress)
todd wait 12 Priya to confirm numbers  # waiting, and on what
todd review 12                         # in review    (Jira → In Review)
todd done 12 sent the sheet            # done         (Jira → Done, follow-ups, reply in Slack)
todd move 12 waiting --no-jira         # any state to any other; leave Jira be this time
todd following                         # what you're keeping an eye on
todd followup                          # everything you owe people
todd links 12                          # every link, one per line, nothing else
todd reply 12                          # where to reply, with an offer to draft it
```

### What you see

`todd` (or `todd now`) is what you can act on: follow-ups that are due, what you're doing,
what's to do and not blocked, and anything not filed yet. Each task says which project it's part
of. What's waiting, in review, blocked or only followed is counted underneath, not listed.

```text
↪ Follow-ups due 1
   ↪3  Warn Mike R that review slips                                         today Sep 30
       #11 Write the RFC on tenant isolation

● Doing 1
!   #7  Ship the Torii webhook  ▸ Torii webhook migration      Fri       PLAT-500      2h

● To do 2
!  #10  Send Priya the Q3 migration numbers                    tomorrow  PLAT-77 · Slack  1d
    #9  Update the partner docs  ▸ Torii webhook migration                             3d

Not yours to act on now: 2 waiting · 1 in review · 4 blocked · 1 following
todd ls shows everything, by project
```

`todd ls` is everything open: each project with its state and its tasks in order (blocked ones
too, with what blocks them), then the tasks that aren't part of a project under **No project**.
`--following` adds what you're following, `--all` adds what's done or dropped, and `--area` and
`--person` narrow it (they work on `todd now` too). Lists use the full width of your terminal
and wrap rather than cut anything off; when it's narrow, a task's links go under its title.

### States

`todd states` prints this, with more detail:

| State | Meaning |
| --- | --- |
| `inbox` | Captured but not filed by Claude yet. |
| `todo` | Yours to do, not started. |
| `doing` | You're working on it. |
| `waiting` | You've done your part and are waiting on someone or something. |
| `in_review` | Your work is done and out for review. |
| `done` | Finished. |
| `dropped` | Not doing it after all. |
| `following` | Not yours (yet): something you're keeping an eye on. |

**Blocked** isn't a state: a task is blocked while any task it waits on is still open, and
stays out of `todd now` until it's free.

`todd move` goes from any state to any other, and `start`, `wait`, `review`, `done`, `drop`,
`follow` and `reopen` are shortcuts. Each takes a note (`todd done 12 shipped it`), `-y` to
update Jira without asking, `--no-jira` to leave Jira alone without asking, and `--local` to
leave Jira and Slack alone. Adding a task never moves its tickets. If one should match, use
`todd push`.

Two things work differently. A **project** is never moved itself: its state comes from its
tasks (you can drop it, which drops its open tasks, and reopen it after that). And something
you're **following** isn't yours to move along: it's always a task of its own, outside any
project, and from there it can only become to do (`todd reopen 12`, when it lands on you), done
or dropped.

### Changing Jira

Every time a state change would move a ticket, todd shows the move and asks about it:

```text
  Jira PLAT-412  In Progress → Done
  Move PLAT-412 to Done?   No   Yes    ←/→ Enter
```

The highlight starts on **No**. ←/→ (or Tab) move it and Enter picks; typing `y` or `n` jumps
to that answer but still waits for Enter. Anything typed before the question appeared is
ignored, so an early Enter can't answer it. Several tickets get a question each. Where todd
can't ask (a script, a pipe), it leaves Jira alone and says how to catch up: `-y`, or
`todd push 12` later. The other choices todd offers (reply in Slack, follow-ups) work the same
way.

Other commands:

| Command | What it does |
| --- | --- |
| `todd now [--area name] [--person name]` | What you can act on. Plain `todd` is the same. |
| `todd ls [--all] [--following] [--area name] [--person name]` | Everything open, by project. |
| `todd states` | Every state and what it means, plus how projects, blocking, following and follow-ups work. |
| `todd triage [id]` | File a task again (say, after adding links), or everything still in your inbox. |
| `todd link 12 <links…> [-q text]` | Attach more links. |
| `todd note 12 <text>` | Add to the timeline. |
| `todd edit 12 --due fri --priority high --area none --in 7 …` | Correct what Claude filed; `--in` moves it into project #7 (or `none`). |
| `todd push 12` | Push the task's state to Jira: move its tickets to match (asking about each). |
| `todd pull [id] [-y] [--links-only]` | Pull from Jira and GitHub: re-read the links and update the task to match (see below). |
| `todd role 12 3 reply` | Say what link 3 is for. `reply` marks where you'll reply; refiling keeps it. |
| `todd links 12 [--labels]` | Print the links bare, one per line, so your terminal can make them clickable. |
| `todd open 12 [3]` | Open a task's links (or just link 3) in Slack or your browser. |
| `todd followup add 12 "Tell Theo" [--to Theo] [--on fri \| --when done] [--unless in_review]` | Add a follow-up yourself. |
| `todd followup done\|drop\|snooze N` | Close a follow-up (↪N), or move it to another day (`snooze 4 +7`). |
| `todd projects [--all]` | Every project, with all its tasks in order (finished ones too) and what's blocking what. |
| `todd add "…" --in 7 [--after 14,15 \| none]` | Add a task to project #7. By default it waits on the project's last open task. |
| `todd add --project "…"` | Capture something as a project even when only its first task is clear. |
| `todd block 16 --on 15` / `todd unblock 16 [--on 15]` | Say a task can't start until another is done, or stop it waiting. |
| `todd drop 7` / `todd reopen 7` | For a project: drop it and its open tasks (after asking), or bring them back. |
| `todd nick [login] [name…] [--remove]` | List, set or forget nicknames. |
| `todd config [--init] [--edit]` | Where todd keeps things, and what it's set to do. |
| `todd doctor [--claude] [--jira KEY] [--pr URL]` | Check the tools todd uses, and what it makes of a real ticket or PR. |

### Before anything is saved

In a terminal, `todd add` and `todd triage` show what Claude would file (the card, what each
link is for and, for a project, its tasks and where each link goes) and ask what to do:

```text
  File it?   Add it   Change it…   Leave it in the inbox    ←/→ Enter
```

**Change it…** asks what to change, in your own words. Claude gets its previous filing and your
change, and you see the new version. Changes add up, so you can keep going. **Leave it in the
inbox** keeps the capture unfiled for `todd triage` later. `-y` files without the preview; so
does anything without a terminal, like a script.

### Keeping a Slack message with its link

When you `todd add` a Slack link in a terminal, todd asks you to paste the message: press Enter
to skip, or paste and finish with Ctrl-D (or two empty lines). You can also pass it with
`-q "…"`, one per Slack link and in order.

In the editor (`todd add -e`) or piped text, put each link on its own line. Whatever sits under a
link, up to the next link, is that link's text:

```text
Reply to Priya with the Q3 migration numbers

https://acme.slack.com/archives/D024BE91L/p1790776800123456
Hey, can you send me the Q3 migration numbers before Thursday's sync?

PLAT-412
```

Slack links you capture are context by default. Say where you'll need to reply with
`--reply-in <link>` when you add the task, or `todd role 12 3 reply` afterwards.

### Projects

A project is work you move forward through one or more tasks. A task doesn't have to be in a
project; those that aren't are listed under **No project**. Describe the chain in one capture,
and Claude files the project and its tasks:

```sh
todd add "Waiting on reviews for this stack" <PR url> PLAT-101 \
  "as soon as that's done, a bug bash" PLAT-102 "then configure the live feature for Acme" PLAT-103
```

```text
▸ #1 Launch the live feature for Acme  waiting · 3 of 3 tasks open
   1  ●  #2  Get the cluster-b stack reviewed and merged    waiting    PLAT-101 · stack 530 (3 PRs)
             waiting on reviews from Nik (2), Will M
   2  ◌  #3  Run the bug bash                               blocked    PLAT-102
             blocked by #2 Get the cluster-b stack reviewed and merged
   3  ◌  #4  Configure the live feature for Acme            blocked    PLAT-103
             blocked by #3 Run the bug bash
```

**A project's state is never set by hand.** It comes from its tasks: doing if any task is under
way; otherwise in review, to do, then waiting, among the tasks that aren't blocked; blocked when
every open task waits on another; done when all its tasks are done. Finish the last task and
todd tells you the project is done. Add a task to a finished project and it's open again. The
one thing you do to a project itself is drop it (`todd drop 1`), which drops its open tasks
after asking, and `todd reopen 1` brings those back.

**Tasks wait on whichever tasks they need.** Usually that's one after another, but several tasks
can wait on the same one and run at the same time, one task can wait on several, and a task can
wait on nothing. Say it in the capture ("then docs and the support briefing together, then the
announcement"), or set it yourself with `todd block 16 --on 15` and `todd unblock 16`. A blocked
task stays out of `todd now`; when you finish or drop the task in its way, todd tells you what
that unblocked.

Each task keeps its own links, so its own Jira ticket moves (after asking) when it does. Links
and follow-ups on the project itself act on the project's state: "tell sales when it's live"
comes due when its last task is done.

A project always has at least one task. `todd add --project "Acme launch"` makes a project out
of something whose later tasks aren't clear yet, starting with its first task; add more with
`todd add "…" --in 1`, or move an existing task in with `todd edit 16 --in 1`.

### Follow-ups

Mention them the way you'd say them, and Claude writes them down:

| You say | todd keeps |
| --- | --- |
| "…and tell Theo A when I'm done" | ↪ Tell Theo A that PLAT-123 is done, due when the task is done |
| "told Mike R I'd ask him to review it this week, but it may slip" | ↪ Ask Mike R to review the RFC, due when it's in review. ↪ Tell Mike R review slips to next week, due Friday unless it's in review by then. |
| "Following <link>…" | ↪ Check in on it, due on the day you said, or two weeks out |

Follow-ups that are due show at the top of `todd`, wherever their task is, even one you're
only following. When a task reaches the state a follow-up waits for, todd offers to draft the
message with Claude (copied to your clipboard), mark it done, or keep it for later. A dated
follow-up that no longer matters (the doc went out for review before Friday) closes itself.

### Waiting on reviews

Pull requests carry their reviewers: who's been asked and hasn't reviewed yet, who approved, and
who wants changes. A waiting task sums up the pending requests across all its PRs, busiest
reviewer first. `todd pull` brings reviewers and statuses up to date.

### Pulling and pushing

Like git: `todd push 12` sends the task's state to Jira, and `todd pull 12` brings the task up to
date with its links. Pull re-reads its tickets and pull requests, notes what changed since todd
last looked (a ticket moved to Done, a stack merged, a reviewer approved), and asks Claude what
that means for the task: its state, next step and what it's waiting on. You see the proposal
first (**Apply it**, **Change it…**, **Skip**); a state change then goes through the usual move,
with its Jira question, follow-ups and unblocking. `todd pull` on a project pulls its tasks too;
`todd pull` alone pulls everything open, asking Claude only about tasks whose links changed.
`--links-only` just re-reads the links.

### Pull request stacks

todd uses GitHub's native stacked pull requests (public preview since July 2026). When you link
a pull request, todd asks GitHub whether it's in a stack. If it is, todd reads every member in
one query and adds the ones you didn't link, numbered bottom to top. Claude sees them as one
stack: what has landed, what's waiting on review, what has changes requested or failing checks,
and which threads are unresolved. `todd show` lists them together.

### Tickets named in pull request titles

When a pull request's title has a Jira key in parentheses, as in `(PLAT-412) Move the workers`,
`fix(PLAT-412): move the workers` or `Move the workers (PLAT-412)`, todd adds that ticket to the
task as the ticket that tracks the pull request. That goes for every pull request in a stack,
and in a project the ticket goes to the same task as its pull request. So this is enough:

```sh
todd add "waiting on review" https://github.com/acme/billing/pull/86
```

```text
  ✓ acme/billing#86 (2 in its stack) · (PLAT-412) Move billing-worker to cluster-b · open · Priya
  + PLAT-412 · Migrate billing workers to the new cluster · In Progress · from the title of #86
```

A key is capitals, a hyphen and a number, and it's the first thing in parentheses that is
exactly that. Other things look the same ("(UTF-8)"), so the ticket is only added if Jira can
read it, or if its project is one you list under `[jira] keys`. A ticket you linked yourself
isn't added twice, `todd pull` picks up a ticket added to a title later, and as always, adding
a task never changes anything in Jira.

### Nicknames

```sh
todd nick priya-n Priya                # @priya-n is Priya
todd nick acme/platform-reviewers "Platform reviewers"
todd nick                              # everyone
todd nick priya-n --remove
```

Nicknames live in `~/.config/todd/nicknames.toml`, which todd writes itself. They're used when
todd shows pull request authors and reviewers, when it tells Claude who's who, and by
`todd ls --person`, which matches a login or a name.

## Configuration

`todd config --init` writes `~/.config/todd/config.toml` with every setting commented out. The
ones you're most likely to want:

```toml
[jira]
site = "acme.atlassian.net"   # so bare keys like PLAT-412 become links
keys = ["PLAT", "OPS"]        # project keys to spot inside your task text

# The Jira status a ticket moves to when its task enters each state.
[jira.status]
doing = "In Progress"
in_review = "In Review"
done = "Done"

# Projects with another workflow can override any state; "" leaves Jira alone.
[jira.projects.OPS.status]
in_review = ""
done = "Closed"

# Areas of work Claude should file tasks under.
[areas]
platform = "Infrastructure, CI, migrations"
hiring = "Interviews, debriefs, hiring loops"
```

`[claude]` takes `model`, `effort` (default `low`), `timeout` and extra `args` for `claude -p`.
`[slack]` controls whether todd asks for message text and which states offer to reply.
`[following] check_in_days` (default 14) sets when to check back on something you follow, if you
didn't say. `[jira.status]` can map any state, `following` and `waiting` included.

When a task has several Jira links, only the one Claude marked as *the ticket* moves. A lone
Jira link moves unless Claude called it background.

## Where things live

| | Default | Override |
| --- | --- | --- |
| Tasks | `~/.config/todd/todd.sqlite` | `--db` or `$TODD_DB` |
| Settings | `~/.config/todd/config.toml` | `--config` or `$TODD_CONFIG` |
| Nicknames | `nicknames.toml` beside the settings | |

`$XDG_CONFIG_HOME` is respected.

## Development

```sh
uv run pytest
uv run ruff check && uv run ruff format --check
uv run ty check
```

The tests never reach Jira, Slack, GitHub or Claude. `tests/conftest.py` fakes every program todd
runs, using made-up fixtures in the same shapes the real tools return. For checks against the
real tools, see [docs/field-tests.md](docs/field-tests.md).

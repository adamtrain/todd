# Field tests

todd was built on a machine without `acli`, a work Jira or a work Slack, so these parts have
only been checked against made-up fixtures:

- reading and moving Jira tickets through `acli`
- Slack links opening in the Slack app
- running `claude -p` under your employer's Claude Code setup
- pull request stacks on your work GitHub, if that's GitHub Enterprise

These *were* checked live: headless `claude -p` with todd's schema, and reading real native
stacks (`github/gh-stack`, `cli/cli`) through `gh`. The six example captures in section 8 also
ran through the real Claude against a stand-in `acli` that served made-up tickets.

Run these on the work Mac. If something fails, send back the command, its output, and
`todd doctor` (redact anything sensitive).

## 0. Just say it

Try a few things the way you'd naturally say them, with your real tasks:

- [ ] `todd "move the <something> task to done and start the next one"`: the plan names the
      right tasks; Enter runs it; Jira is still asked about.
- [ ] `todd "what am I waiting on?"` and `todd "what can I work on?"`: show things without
      asking first.
- [ ] `todd states`, then phrase a request with those words ("park the bug bash, it's waiting
      on Dana"): Claude picks the right state.
- [ ] `todd "make <task> high priority and due friday"`: becomes an edit.
- [ ] `todd "remind me to …"`: becomes a capture, with the usual filing preview.
- [ ] Something ambiguous: Claude asks which task you mean.
- [ ] Something that needs a result first, like `todd "add a task to <do something>, make it
      wait on <an existing task>, and start it"`: the first plan ends with "then: …", and after
      it runs a second plan ("Next, todd will") uses the new task's real number. You can stop
      between the two.
- [ ] Note anything it gets wrong; the command reference and prompt are easy to tune.

## 1. Setup

```sh
uv tool install --editable .
todd doctor --claude
```

Expect ✓ for `config`, `database`, `claude`, `claude -p`, `acli`, `jira auth` and `gh`.

- [ ] Everything is ✓. How long did `claude -p` take?
- [ ] If your Claude Code runs hooks (notifications, sounds) or your CLAUDE.md seems to affect
      todd's filings, add `args = ["--safe-mode"]` under `[claude]` (`todd config --edit`) and
      run `todd doctor --claude` again.

## 2. Reading a real Jira ticket

```sh
todd doctor --jira PROJ-123
```

- [ ] Summary, status, type and assignee are right.
- [ ] The description's first line is readable text, not JSON.
- [ ] The URL is right. If it says to set `site`, add `site = "yourcompany.atlassian.net"`
      under `[jira]`.

If anything is off, please send the raw output:

```sh
acli jira workitem view PROJ-123 --json --fields key,issuetype,summary,status,assignee,priority,description | head -c 4000
```

## 3. Configuration

```sh
todd config --init && todd config --edit
```

Set `site`, `keys` (your project keys) and `[jira.status]` to your workflow's exact status
names. Add per-project overrides where workflows differ, then:

```sh
todd config
```

- [ ] The state → status mapping looks right.

## 4. Capturing

Use a real Slack message and a real ticket:

```sh
todd add "what you'd normally jot down" <slack link> PROJ-123
```

When todd asks, paste the Slack message and press Ctrl-D.

- [ ] The ticket line shows ✓ with the right summary and status.
- [ ] The Slack line says "message text kept".
- [ ] The filed card makes sense: title, next step, area, priority, due date, people.
      Note anything Claude gets consistently wrong; that's prompt tuning.
- [ ] `todd show <n>` shows the message under its link, with who wrote it.
- [ ] `todd add -e` opens your editor and captures what you write.
- [ ] Before anything is saved you see "Claude would file this · not saved yet". Pick Change it…,
      ask for something ("call it …, make it high priority"), and the next preview has it.
      Leave it in the inbox leaves it unfiled; `-y` skips the preview.

## 5. Moving a ticket

Use a ticket that's safe to move, ideally a throwaway one.

```sh
todd start <n>       # asks "Move KEY to In Progress?": pick Yes with → then Enter
todd review <n>      # press Enter on No this time: the ticket shouldn't move
todd done <n> -y     # no question; it just moves
todd drop <n> -J     # no question; Jira untouched
```

- [ ] The question starts on No, ←/→ and Enter work, and the line ends up showing your answer.
- [ ] Each Yes moved the ticket in Jira to the mapped status; each No left it alone.
- [ ] Typing Enter quickly, before the question appears, doesn't answer it.
- [ ] Put a status your workflow can't reach into the mapping, then move a task: the error is
      readable, and the todd state still changed.
- [ ] `todd push <n> -y` fixes a ticket that has drifted.
- [ ] Move a ticket in Jira yourself, then `todd pull <n>`: it notices, proposes the matching
      todd state, and applies it when you pick Apply it.

## 6. Replying in Slack

On `todd done <n>` for a task with a Slack link:

- [ ] `o` opens the message in the Slack app, not just the browser.
- [ ] `d` drafts a reply that sounds like you, copies it, and offers to open the thread.

## 7. Pull request stacks at work

```sh
todd doctor --pr <a stacked PR at work>
todd doctor --pr <an unstacked PR>
```

- [ ] The stacked PR lists every member, bottom to top, with status and reviews.
- [ ] If you're on GitHub Enterprise Server and the stack line says "couldn't check", send me
      the detail. todd then reads the PR alone, which is safe, but it misses the rest of the stack.
- [ ] `todd add "review <name>'s stack" <PR url>` adds every PR in the stack, and `todd show`
      groups them.
- [ ] For a PR whose title has a ticket in parentheses, like `(PLAT-412) …` or `fix(PLAT-412): …`:
      `todd doctor --pr <url>` says "ticket in its title: PLAT-412", and `todd add "…" <PR url>`
      (no ticket given) shows `+ PLAT-412 · … · from the title of #N` and files it as the ticket.
      `todd done <n>` then asks about moving it.
- [ ] If your PR titles put the ticket somewhere else (square brackets, no parentheses), tell me
      the shape and I'll match it.
- [ ] A PR with something else in parentheses, like `(WIP)` or `(UTF-8)`, adds no ticket.

## 8. Your six kinds of capture

Try each with real links, and note anything Claude gets wrong.

- [ ] `todd add "Currently waiting on review from this PR stack" <PR> "related to these tickets" <KEY> <KEY>`:
      filed as *waiting*. It isn't in `todd` (it's counted under "Not yours to act on now");
      `todd ls` says "waiting on reviews from …" with the right people, and `todd show` has the
      reviewer table. Is the summary readable when many people are pending?
- [ ] `todd add <KEY> "by Friday, see also" <slack> <slack>` (Enter to skip pasting): the Slack
      links show as *reference*, and `todd done` doesn't offer to reply in them.
      `todd role <n> 2 reply` changes that.
- [ ] The RFC one: two follow-ups, one for asking Mike to review (when in review) and one for
      warning him on Friday, unless it's in review by then.
- [ ] `todd add "Following" <slack> "…might end up on my plate"`: it's not in `todd`, it is in
      `todd following` (and `todd ls --following`) with a check-in date, and the check-in
      shows in `todd` when it's due. `todd start <n>` refuses; `todd reopen <n>` takes it on.
- [ ] `todd add "I need to complete" <KEY> "and tell Theo A when I'm done"`: titled from the ticket.
      On `todd done`, it offers to draft the message to Theo.
- [ ] `todd add "I need to address" <slack> "and tell Mary S when I'm done"`, without pasting:
      todd asks you for a title instead of inventing one.

## 9. Projects and lists

- [ ] Describe a chain in one capture, with a ticket per part, the way you would naturally:
      `todd add "Waiting on reviews for this stack" <PR> <KEY> "then a bug bash" <KEY> "then
      configure the live feature for <customer>" <KEY>`. Claude makes one project with a task
      per part, in order, each with the right ticket (the stack goes with the first).
- [ ] `todd` shows only what you can act on, each task with its project's name; the footer
      counts what's waiting, in review, blocked and followed.
- [ ] `todd ls` shows the project with the right state (waiting, while its first task waits on
      reviews), every task with its state, and loose tasks under "No project".
- [ ] Nothing is cut off in `todd`, `todd ls` or `todd projects`: project names, ticket keys and
      "stack N (M PRs)" are all there in full, at your usual terminal width and a narrow one.
- [ ] `todd done <first>` says what it unblocked and what the project is now; the next task
      appears in `todd`.
- [ ] Finishing the last task says the project is done, without asking anything about the
      project. `todd add "…" --in <project>` opens it again.
- [ ] Describe tasks that can run together ("then docs and the support briefing at the same
      time, then the announcement"): both wait on the same task, and the last waits on both.
- [ ] `todd start <project>` refuses and says why. `todd drop <project>` asks (starting on No),
      then drops its open tasks; `todd reopen <project>` brings them back.
- [ ] A single task is still a single task: Claude doesn't invent tasks of its own.

## 9a. Numbers

Your existing database works as it is: the first command you run tidies the numbers and says
what moved.

- [ ] `todd ls` right after upgrading: if there were gaps, it says "Renumbered: …" first, then
      lists everything numbered from 1.
- [ ] `todd done <n>` on something in the middle of the list: the tasks after it move down by
      one, the line under it says so, and the finished task is the first number after your
      open ones (`todd ls --all`).
- [ ] `todd reopen <that number>` brings it back without moving anything else.
- [ ] `todd add "…"` gives the new task the first number after the open ones, with no
      "Renumbered" line.
- [ ] `todd "finish <this> and start <that>"`: both steps hit the right tasks, and the
      "Renumbered" line comes once, at the end.
- [ ] Does the renumbering ever surprise you (say, typing two commands from one listing)? If
      so, tell me: it could wait until the next time you list things instead.

## 9b. Defer dates and due dates

Your database is upgraded in place the first time you run this version; nothing to reset.

- [ ] `todd defer <n> mon`: the task leaves `todd`, the footer counts "1 deferred", and
      `todd ls` shows it as deferred "until Mon". `todd defer <n> none` brings it back.
- [ ] `todd add "<something>, not before <a date>"`: Claude sets the defer date (the preview
      shows "Deferred until …"), separately from any deadline.
- [ ] In a project, defer the only task that isn't blocked: `todd ls` shows the project as
      "deferred until <date>". Defer two tasks with different dates and it shows the sooner.
      `todd defer <project> mon` refuses and says why.
- [ ] `todd start <a deferred task>` says "No longer deferred."
- [ ] `todd "put the <task> off until next month"` becomes a defer.
- [ ] Deadlines in `todd` and `todd ls` now read "due Fri", "due tomorrow", "overdue 2d".

## 9c. Watching

- [ ] `todd watch` in one terminal, then in another: add a task, start one, finish one, defer
      one. Each change shows up within a second or so, redrawn in place, with "updated HH:MM:SS"
      moving on.
- [ ] Resize the watching window: it lays itself out again. Make it very short: the last line
      says how many more lines there are.
- [ ] Ctrl-C leaves cleanly and gives the terminal back as it was.
- [ ] Does it work in your usual setup (tmux pane, split window, the terminal you'd really
      leave it in)? Any flicker?

## 10. Moving, links and Jira failures

- [ ] `todd move <n> <state>` works from any state to any other (except out of following,
      which only goes to to do, done or dropped). `--no-jira` leaves the ticket.
- [ ] Map a state to a status your workflow can't reach and move a task there. todd says it
      failed and prints the ticket's link on a line of its own. Can you ⌘-click it?
- [ ] `todd links <n>`: can you ⌘-click every line, including long URLs?
- [ ] `todd pull`: statuses and reviewers catch up with what changed in Jira and GitHub, and
      Claude is only asked about tasks whose links changed.

## 11. Nicknames

```sh
todd nick <a colleague's login> <what you call them>
```

- [ ] `todd show` and `todd doctor --pr` show the name instead of the login.
- [ ] `todd ls --person <name>` and `--person <login>` both find their tasks.

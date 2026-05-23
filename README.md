(All code in this repository has been reviewed by humans, this README has been written by a human)

# Lup

A meta repository for speed-boosting your Claude Code development and create self-improving [ClaudeAgentSDK] applications

<img width="1535" height="863" alt="image" src="https://github.com/user-attachments/assets/d5159e28-1669-433b-8f89-e012c3abfa1c" />
<img width="1532" height="554" alt="image" src="https://github.com/user-attachments/assets/6f272a6e-71b6-4720-89bf-6b811081343d" />

# Why this repo?

I believe that claude code may be underappreciated right now. Not just the agent, but its [SDK](https://platform.claude.com/docs/en/agent-sdk/overview). The SDK of claude code allows it to:

- Think through a task by using many tools to fetch information, and decompose it carefully
- Delegate part of its task to other agents through tool calls that augment or gatekeep the results
- React in real time through auto-deny hooks and get_current_state tools

... all of that through your claude code subscription, without having to pay any extra API cost or setup.

The basic pattern is to create a ClaudeAgentSDK client, connect many tools to it (like fetching from APIs, searching the web, executing code, interacting with the world) and let claude code decide when to call them.

I've found developping those SDK applications with the help of Claude Code to work very well, as well as using Claude Code to improve the whole development scaffolding, using it to add /-commands that speeds up my development, to document general principles and developping devtools to help me or itself navigate it faster. More importantly, Claude Code can review results from past sessions, and tweak the agent based on it, be it its tools, prompts, or all aspect of the pipeline and workflow.

This repository is focused on this sort of agent-improvement and meta-self-improvement. It contains tools for storing the traces of all past agents, versionning the current agent, commands for reviewing them and seeing how to improve based on it, common multi-agents pattterns I've found useful, as well as meta-commands to add commands or review your own development with Claude Code.

Over writing and reusing this technique over the past month, I have come to find that having a template and plugin as a base can really speed up the development and the coherence of Claude. This repository is a sort of extract of all the common patterns and plugin command and scripts I have found useful.

It is a template to help bootstrap this pattern and create your own ClaudeAgentSDK easily.

# Examples

Some examples of things I'm using this self-scaffolding for (still in early WIP):

- [joy.void.joy-bot]: Not yet opensource. A forecasting agent written for the [FutureEval] tournament. Basically this repo with news-searching and many API-fetching tools, and using the feedback-loop mechanism on newly resolved/retrodicted forecast
- [harmon]: real-time discord bot focused on presence and reactivity/helpfulness, as well as background tasks. The tools here are things like reply, follow_ups, sleep, and contains a gate that forbids it from replying if it hasn't read the new messages first.
- [mettle]: A bot whose main tool is writing its own tools
- [botc]: Having bots compete with one another while playing [Blood on the Clocktower]

But you could use it for so much more. Real-time monitoring, mathematical proofs or formal verification or for [[AIMO3]], anything that can be automated where the kind of resources or tools it needs is easy to explore and refine.

# Getting started

To start using this repo either:

- For a fresh repository: Use the "Use this template" button on github, or clone this repository. In the newly cloned repository, use /lup:init [description of your project] or /lup:brainstorm to first flesh out the broad shape of it
- For an already existing repository, clone lup inside it, and either use /lup:install to install the bare plugin, or /lup:install --interactive to install the

You will need to install [uv] for python management and [fzf] for fuzzy-file matching. Docker is an additional dependency if you plan to use the sandboxing capabilities.

The intended workflow while using this repository is to:

- Have it cloned in a bare repo
- Creating a worktree to tree/main: ``git clone
- When working on a new feature, branching off from it with `lup-devtools worktree create <branch-name>`
- Going into this new branch, and working on it there
- Then /lup:commit it
- When it works and you've tested it works well /lup:rebase it
- Review it in github before merging it
- Call /lup:close on the merged branch or /lup:clean-gone on any branch (like main) to keep the worktree clean

# Overview

This repository contains many elements and code template that are designed to make creating your own scaffolding with ClaudeAgentSDK seamless:

- Code template and utilities to create a ClaudeAgentSDK with appropriate tools and hooks from scratch
- Many quality of life improvement to the claude code experience through a lup plugin
- devtools aimed at both human use and agent use
- Feedback loop and note-taking mechanisms for auditing and improving your agent

## Intended workflow

### Meta development

### Worktree management

### Feedback loop

# More thorough description

## Code template

### lib

src/lup/lib is the [[[]]]

### agent

template [[[]]]

### Environment

[[[]]]

## Claude code plugin

This repository contains many quality of life improvements over the barebone claude code experience:

- Hooks for automatically aproving and denying edition and code executions: I am too worried with potential prompt injections and hallucination to let Claude Code run python unprompted. Likewise, I have found that letting claude code in auto-edit mode makes a patch of code that's quite unreadable with many questionable decision, no matter the initial direction and content of Claude.md. On the other hand, manually reviewing everything is exhausting and leads to counterproductive decision-fatigue where you just approve everything repeatedly. I have found that auto-denying python calls while pre-approving investigative commands (see #devtools) means it's manageable, and same for auto-accepting small edits.
- Commands and meta-commands for modifying your experience whenever you find a pain point (like /lup:add-command or /lup:meta)
- subagents specialized in reading the traces and the different versions of your project, and understanding the strength of one version over another
- fzf fuzzy matching for @ file references

### Subagents

### Hooks

### Claude commands

To speed up development, many claude commands and meta-commands are built in this repository:

- add-command
- modify-command

- bump

- commit
- rebase
- merge-conflict
- clean-gone
- close

- create-investigator
- debug

- feedback-loop

- hooks

- refactor
- refactor-tools

- update
- import
- brainstorm
- install
- init

## Devtools

### Agent

- api.py
- trace.py
- feedback.py
- metrics.py
- sync.py
- git.py

### Human

- agent.py
- charts.py
- dev.py
- usage.py

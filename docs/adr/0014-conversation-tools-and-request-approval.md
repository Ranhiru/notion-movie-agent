# Conversation tools preserve enrichment ownership and enforce request approval

The conversational agent uses dedicated Watchlist tools backed by the existing Runtime,
instead of reproducing the enrichment graph's identity resolution, checkpoints, writes,
and human Disambiguation. Conversation state belongs to a Slack thread; enrichment state
continues to belong to a Notion Entry. This preserves durable enrichment and late replies
while allowing a conversation to reference several Entries.

Approval enforcement belongs to the application tool boundary, independently of the model
and agent framework. Requests affecting four or more distinct Entries require explicit
approval across all tool calls; URL Imports always require preview approval. Binding
approval to the exact actions and Entries prevents an agent from splitting a bulk request
into individually permitted writes or expanding an approved batch. Approvals are single-use,
expire after 30 minutes, and completed execution steps persist for recovery. Removal is
soft deletion only.

This deliberately adds durable request bookkeeping rather than relying on prompts or
per-tool approval counts. The extra state is necessary to preserve these rules across
multiple tool calls, retries, and restarts. Execution ordering and recovery details remain
part of the implementation design.

The personal deployment treats all incoming user messages and interactive actions as the user;
there is no Slack user allowlist. IMDb confirmation cards remain bound to their original
thread and Entry, retain expiry and replay checks, and recheck current Notion state.

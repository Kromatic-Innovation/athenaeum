## Relevance hints (what this deployment wants kept)
This deployment has standing capture rules that say these kinds of fact are
worth keeping:
<<rules>>

Treat each line as a hint when deciding whether an observation is worth
returning: a fact that clearly fits one of them leans toward being kept. A hint
is not an instruction to keep. It does not override the observation filter
above, and you must still leave out anything the filter says not to capture,
such as ephemeral or transient detail.

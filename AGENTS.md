<!-- tracelayer-agent-invariant:v2 -->
This repository uses mandatory semantic traceability. Trace integrity is part of the Definition of Done. Follow the repository traceability skill and any trace instructions injected by hooks. Do not invent trace fields, replace stable IDs during refactors, or remove markers to silence validation. Before completing implementation work, `trace verify --changed` must pass under the active policy.

TRACE AUTHORING IS MANDATORY: whenever a Write/Edit/Create introduces or materially changes a behavioral boundary, that boundary must be trace-accounted in the same change (preserve an existing trace ID, add a canonical `trace:v1` marker, or record an explicit `# trace:exempt` with a reason). Do not write new product behavior first and plan to trace it later — in the default remind mode the pre-mutation hook allows the write and records an obligation (Stop/merge/CI enforce it); strict repositories may opt into block mode, where hooks deny untraced new behavior before it is written. Deleting traced behavior with live incoming edges and the Stop gate always enforce regardless of mode. Example:

    # trace:v1 id=impl.<slug> work=<WORK-ID> satisfies=<REQ-ID>
    def rotate_token(...): ...

<!-- bugcorpus:start -->
This repository uses Bug Corpus.
For confirmed defects or requests to search for similar bugs,
use the repository's bug-corpus skill and `bugcorpus`.
See skills/bug-corpus/SKILL.md.
<!-- bugcorpus:end -->

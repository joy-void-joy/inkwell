"""A work of many parts, held as a tree the pipeline can run against.

The writing pipeline takes source material and produces one piece. A textbook
is not one piece: the AI Safety Atlas is nine chapters of seven-or-so section
files, each holding several subsections, and the unit anybody actually revises
is the subsection. Nothing here writes anything — this is the tree, its
identities, and the state each node is in, so that a run can be about one
subsection while the book it belongs to stays coherent around it.
"""

# Entry, Active Monitoring and Exit

Entry and Exit are separately trained policy artifacts. Entry chooses immediate, pullback, confirmation or wait. `ActiveSignalAgent` revisits ACTIVE/UPDATED signals on new same-asset evidence and moves them through UPDATED to EXIT_PENDING when direction reverses, learned exit probability crosses its gate, or risk breaches its boundary.

Post-exit outcomes classify good, late and false-reversal exits and feed failure attribution, datasets and future challengers. No broker execution is present.

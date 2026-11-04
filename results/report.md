Model: `gemini-flash-lite-latest`, temperature 0.0, 55 questions, repeats per system: A_full_norepair=1, B_full_repair=1, C_retrieved_repair=1

## Accuracy (strict result match; mean over repeats [min-max])

| System | Overall | easy | medium | hard | clarify | refuse | Lenient overall |
|---|---|---|---|---|---|---|---|
| A_full_norepair | 100.0% [100%-100%] | 100.0% [100%-100%] | 100.0% [100%-100%] | 100.0% [100%-100%] | 100.0% [100%-100%] | 100.0% [100%-100%] | 100.0% |
| B_full_repair | 98.2% [98%-98%] | 100.0% [100%-100%] | 100.0% [100%-100%] | 92.9% [93%-93%] | 100.0% [100%-100%] | 100.0% [100%-100%] | 98.2% |
| C_retrieved_repair | 98.2% [98%-98%] | 100.0% [100%-100%] | 93.3% [93%-93%] | 100.0% [100%-100%] | 100.0% [100%-100%] | 100.0% [100%-100%] | 98.2% |

n = 55 questions per run. 95% Wilson CI for a single run at 80% accuracy is about 68%-88%: differences smaller than this need the paired tests below.

## Failure breakdown (pooled over repeats; heuristic auto-labels, see failure_review.csv)

| Failure type | A_full_norepair | B_full_repair | C_retrieved_repair |
|---|---|---|---|
| wrong_filter | 0.0 | 1.0 | 0.0 |
| wrong_ordering_or_limit | 0.0 | 0.0 | 1.0 |

(values = average number of failing questions per run)

Questions failed by all systems in all repeats: none

## Is the difference real? (paired tests)

**A_full_norepair -> B_full_repair**: mean accuracy change -1.8%; 95% bootstrap CI [-5.5%, +0.0%]; McNemar discordant pairs: B_full_repair better on 0, A_full_norepair better on 1; exact p = 1.000.
  run-to-run spread of overall accuracy: A_full_norepair=0.0%, B_full_repair=0.0%; flip rate (questions whose outcome changes between repeats): A_full_norepair=0%, B_full_repair=0%.
  => Verdict: NOT DISTINGUISHABLE FROM NOISE at this sample size

**B_full_repair -> C_retrieved_repair**: mean accuracy change +0.0%; 95% bootstrap CI [-5.5%, +5.5%]; McNemar discordant pairs: C_retrieved_repair better on 1, B_full_repair better on 1; exact p = 1.000.
  run-to-run spread of overall accuracy: B_full_repair=0.0%, C_retrieved_repair=0.0%; flip rate (questions whose outcome changes between repeats): B_full_repair=0%, C_retrieved_repair=0%.
  => Verdict: NOT DISTINGUISHABLE FROM NOISE at this sample size

## Cost, latency, safety

| System | LLM calls/q | input tok/q | output tok/q | mean latency s | p95 latency s | guard caught writes |
|---|---|---|---|---|---|---|
| A_full_norepair | 1.00 | 2523 | 110 | 3.60 | 8.83 | 0 |
| B_full_repair | 1.02 | 2574 | 117 | 4.15 | 11.20 | 0 |
| C_retrieved_repair | 1.00 | 1858 | 108 | 3.44 | 16.30 | 0 |
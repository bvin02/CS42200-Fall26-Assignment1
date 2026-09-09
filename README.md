# CS 422 - Assignment 1

Bhavin Gupta
Repository: https://github.com/bvin02/CS42200-Fall26-Assignment1

This project runs the ping and traceroute experiments from `assignment-1.pdf`
and creates the plots and report automatically.

## Run

Requires Python 3.10+, `ping`, and `traceroute`.

```bash
./run.sh --origin-label "Purdue University, West Lafayette, IN" \
  --origin-lat 40.4237 --origin-lon -86.9212 \
  --repo-url https://github.com/bvin02/CS42200-Fall26-Assignment1
```

The script gets the current server list if `inputs/ips.txt` is missing. It then:

1. Pings every IP plus the machine's public IP.
2. Looks up each IP's coordinates and calculates distance from Purdue.
3. Runs traceroute in random order until five destinations respond.
4. Creates all CSV/JSON files, raw logs, PDF plots, and the final report.

The saved Purdue run used five ping packets and three traceroute probes per hop.
Nonresponsive servers and `*` hops are kept as missing data.

## Main files

- `experiment.py` - experiment and plotting code
- `inputs/ips.txt` - IP address input
- `results/report.pdf` - final report
- `results/plots/` - the three required PDF plots
- `results/ping.csv` - ping RTTs, coordinates, and distances
- `results/traceroute_hops.csv` - traceroute hop results
- `results/raw/` - original ping and traceroute outputs

Run the tests with:

```bash
python3 -m unittest discover -s tests -v
```

## Submit

Submit `results/report.pdf` using the course submission location. Make sure this
repository is public or that the instructor and TAs can open it. The report
contains the repository link and code line references. Keep the complete
repository online so the input, code, results, plots, and raw logs can be checked.

#!/usr/bin/env python3
import argparse
import concurrent.futures as cf
import csv
from datetime import datetime, timezone
import ipaddress
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import shutil
import socket
import statistics
import subprocess
import sys
import textwrap
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
SOURCE = 'https://iperf3serverlist.net/api/servers'
GEO = 'https://ipwho.is/'


def now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n')
    temp.replace(path)


def get_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'CS422-Assignment1/1.0'})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def execute(command, timeout):
    start = now()
    try:
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=timeout, env={**os.environ, 'LC_ALL': 'C'})
        return dict(command=command, started=start, output=proc.stdout,
                    returncode=proc.returncode, timed_out=False)
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ''
        if isinstance(output, bytes):
            output = output.decode(errors='replace')
        return dict(command=command, started=start, output=output,
                    returncode=None, timed_out=True)
    except OSError as exc:
        return dict(command=command, started=start, output=str(exc),
                    returncode=None, timed_out=False, error=str(exc))


def resolve(host):
    try:
        return [str(ipaddress.ip_address(host))], None
    except ValueError:
        code = ('import socket,json,sys; '
                'print(json.dumps(sorted({x[4][0] for x in '
                'socket.getaddrinfo(sys.argv[1],None,0,socket.SOCK_DGRAM)})))')
        result = execute([sys.executable, '-c', code, host], 20)
        try:
            return json.loads(result['output']), None
        except (ValueError, TypeError):
            return [], result['output'] or 'DNS timeout'


def fetch(args):
    data = get_json(SOURCE)
    if not isinstance(data, list) or not data:
        raise ValueError('Unexpected or empty server list')
    folder = ROOT / 'inputs'
    folder.mkdir(exist_ok=True)
    save_json(folder / 'servers_snapshot.json', {'source': SOURCE, 'retrieved': now(), 'servers': data})
    hosts = sorted({str(row['ip']).strip() for row in data})
    addresses, mapping = set(), []
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        for host, (ips, error) in zip(hosts, pool.map(resolve, hosts)):
            addresses.update(ips)
            mapping.append(dict(host=host, addresses=ips, error=error))
    save_json(folder / 'dns_resolution.json', {'retrieved': now(), 'records': mapping})
    (folder / 'ips.txt').write_text('# Snapshot from ' + SOURCE + '\n' +
                                    '\n'.join(sorted(addresses)) + '\n')
    print(f'Saved {len(addresses)} unique IPs from {len(hosts)} entries; '
          f'{sum(bool(x["error"]) for x in mapping)} DNS failures.', flush=True)


def read_ips(path):
    ips = []
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        token = line.split('#', 1)[0].strip()
        if not token:
            continue
        try:
            ip = str(ipaddress.ip_address(token))
        except ValueError as exc:
            raise ValueError(f'{path}:{n}: expected one IPv4/IPv6 address, got {token!r}') from exc
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise ValueError('Input file contains no IP addresses')
    return ips


def parse_ping(output):
    match = re.search(r'(?:round-trip|rtt)[^=\n]*=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', output)
    loss = re.search(r'([\d.]+)% packet loss', output)
    sent = re.search(r'(\d+) packets transmitted', output)
    received = re.search(r'(\d+) (?:packets )?received', output)
    return dict(min_ms=float(match[1]) if match else None,
                avg_ms=float(match[2]) if match else None,
                max_ms=float(match[3]) if match else None,
                stddev_ms=float(match[4]) if match else None,
                loss_pct=float(loss[1]) if loss else None,
                sent=int(sent[1]) if sent else None,
                received=int(received[1]) if received else None)


def ping_ip(ip, args, out):
    v6 = ipaddress.ip_address(ip).version == 6
    mac = platform.system() == 'Darwin'
    binary = 'ping6' if mac and v6 else 'ping'
    command = [binary] + (['-6'] if v6 and not mac else []) + ['-n', '-c', str(args.count)]
    if not (mac and v6):
        command += ['-W', str(int(args.wait * 1000)) if mac else str(args.wait)]
    command += [ip]
    result = execute(command, args.count * (1 + args.wait) + 5)
    stats = parse_ping(result['output'])
    status = 'responsive' if stats['avg_ms'] is not None else (
        'timeout' if result['timed_out'] else 'non-responsive')
    if result.get('error') or (result['returncode'] not in (0, 1, 2, None)):
        status = 'error'
    save_json(out / 'raw' / f'ping_{ip.replace(":", "_")}.json', result)
    return dict(ip=ip, status=status, **stats)


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlon/2)**2
    return 6371.0088 * 2 * math.asin(min(1, math.sqrt(a)))


def geolocate(ip, cache):
    key = ip or '_origin'
    if ip and key in cache and cache[key].get('success'):
        return cache[key]
    try:
        data = get_json(GEO + ip)
        data['retrieved'] = now()
        data['source'] = GEO + ip
    except Exception as exc:
        data = dict(success=False, message=str(exc), source=GEO + ip, retrieved=now())
    cache[key] = data
    return data


def parse_trace(output, destination):
    hops = []
    for line in output.splitlines():
        match = re.match(r'^\s*(\d+)\s+(.+)', line)
        if not match:
            continue
        body = match[2]
        addresses = []
        for token in body.split():
            try:
                value = str(ipaddress.ip_address(token.strip('()')))
                if value not in addresses:
                    addresses.append(value)
            except ValueError:
                pass
        samples = [float(x) for x in re.findall(r'(\d+(?:\.\d+)?)\s*ms', body)]
        unreachable = bool(re.search(r'![A-Za-z0-9<]', body))
        hops.append(dict(ttl=int(match[1]), ips=addresses, samples_ms=samples,
                         avg_ms=statistics.mean(samples) if samples else None,
                         min_ms=min(samples) if samples else None,
                         unreachable=unreachable,
                         destination=(destination in addresses and bool(samples) and not unreachable)))
    return hops


def trace_ip(ip, args, out):
    v6 = ipaddress.ip_address(ip).version == 6
    mac = platform.system() == 'Darwin'
    command = ['traceroute6' if mac and v6 else 'traceroute']
    if v6 and not mac:
        command += ['-6']
    if not (v6 and mac):
        command += ['-I']
    command += ['-n', '-m', str(args.max_hops), '-q', str(args.probes),
                '-w', str(max(1, math.ceil(args.wait))) if mac else str(args.wait), ip]
    raw = execute(command, args.trace_timeout)
    hops = parse_trace(raw['output'], ip)
    final = next((h for h in hops if h['destination']), None)
    if final:
        hops = [h for h in hops if h['ttl'] <= final['ttl']]
    save_json(out / 'raw' / f'trace_{ip.replace(":", "_")}.json', raw)
    return dict(ip=ip, status='complete' if final else ('error' if raw.get('error') or (raw['returncode'] not in (0, None) and not hops) else 'timeout' if raw['timed_out'] else 'non-responsive'),
                hop_count=final['ttl'] if final else None,
                final_rtt_ms=final['avg_ms'] if final else None, hops=hops,
                protocol='UDP' if mac and v6 else 'ICMP')


def write_csv(path, rows, fields):
    with Path(path).open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore',
                                lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def run(args):
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    ips = read_ips(args.input)
    cache_file = ROOT / 'inputs' / 'geolocation_cache.json'
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    origin = geolocate('', cache)
    own = args.own_ip or origin.get('ip')
    if not own:
        raise ValueError('Could not discover public IP; supply --own-ip and --origin-lat/--origin-lon')
    own = str(ipaddress.ip_address(own))
    if args.own_ip:
        origin = geolocate(own, cache)
    lat = args.origin_lat if args.origin_lat is not None else origin.get('latitude')
    lon = args.origin_lon if args.origin_lon is not None else origin.get('longitude')
    if lat is None or lon is None:
        raise ValueError('Origin geolocation unavailable; supply --origin-lat and --origin-lon')
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError('Invalid origin latitude/longitude')
    targets = list(dict.fromkeys(ips + [own]))
    meta = dict(started=now(), platform=platform.platform(), python=sys.version,
                source=SOURCE, input=str(Path(args.input).resolve()), own_ip=own,
                origin=dict(latitude=lat, longitude=lon, city=args.origin_label or (origin.get('city') if args.origin_lat is None else 'manual origin'),
                            source='manual coordinates' if args.origin_lat is not None else GEO,
                            caveat=('User-supplied approximate coordinates.' if args.origin_lat is not None else 'Approximate IP geolocation; may locate ISP/VPN exit, not physical machine.')),
                config=vars(args).copy())
    meta['config'].pop('func', None)
    save_json(out / 'metadata.json', meta)
    shutil.copyfile(args.input, out / 'input_ips.txt')
    print(f'Pinging {len(targets)} IPs including own public IP; {args.count} probes each.', flush=True)
    rows = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(ping_ip, ip, args, out): ip for ip in targets}
        for future in cf.as_completed(futures):
            rows.append(future.result())
            if len(rows) % 20 == 0 or len(rows) == len(targets):
                print(f'  Ping {len(rows)}/{len(targets)}; {sum(r["status"] == "responsive" for r in rows)} responsive', flush=True)
    rows.sort(key=lambda r: targets.index(r['ip']))
    save_json(out / 'ping.json', rows)
    print('Looking up coordinates (cached; paced requests).', flush=True)
    for i, row in enumerate(rows):
        cached = row['ip'] in cache and cache[row['ip']].get('success')
        geo = geolocate(row['ip'], cache)
        row.update(own_ip=row['ip'] == own, latitude=geo.get('latitude'), longitude=geo.get('longitude'),
                   city=geo.get('city'), country=geo.get('country'), geo_source=geo.get('source'),
                   geo_error=None if geo.get('success') else geo.get('message', 'lookup failed'))
        row['distance_km'] = (0.0 if row['own_ip'] else haversine(lat, lon, row['latitude'], row['longitude'])) if geo.get('success') else None
        if (i+1) % 20 == 0 or i+1 == len(rows):
            print(f'  Geolocation {i+1}/{len(rows)}', flush=True)
            save_json(cache_file, cache)
        if not cached:
            time.sleep(0.65)
    save_json(cache_file, cache)
    save_json(out / 'geolocation.json', {ip: cache.get(ip) for ip in targets})
    save_json(out / 'ping.json', rows)
    write_csv(out / 'ping.csv', rows, list(rows[0]))
    collect_traces(args, out, ips, meta)
    render(args)


def collect_traces(args, out, ips, meta):
    own = meta['own_ip']
    order = [ip for ip in ips if ip != own]
    random.Random(args.seed).shuffle(order)
    save_json(out / 'trace_selection.json', {'seed': args.seed, 'random_order': order,
                                            'policy': 'Try random order until five destinations complete or list exhausted.'})
    traces = []
    print('Tracing a reproducibly shuffled server list until five destinations respond.', flush=True)
    for start in range(0, len(order), args.trace_workers):
        if sum(t['status'] == 'complete' for t in traces) >= 5:
            break
        with cf.ThreadPoolExecutor(max_workers=args.trace_workers) as pool:
            batch = list(pool.map(lambda ip: trace_ip(ip, args, out), order[start:start+args.trace_workers]))
        traces.extend(batch)
        save_json(out / 'traceroute.json', traces)
        print(f'  Traced {len(traces)}; {sum(t["status"] == "complete" for t in traces)} complete', flush=True)
    selected = [t for t in traces if t['status'] == 'complete'][:5]
    meta.update(finished=now(), ping_targets=meta.get('ping_targets', len(ips) + (own not in ips)), completed_traces=len(selected),
                selected_ips=[t['ip'] for t in selected])
    save_json(out / 'metadata.json', meta)
    write_csv(out / 'traceroute_hops.csv', [dict(**{k: v for k, v in h.items() if k not in ('destination', 'ips', 'samples_ms')}, destination=t['ip'],
                destination_response=h['destination'], status=t['status'],
                ips=';'.join(h['ips']), samples_ms=';'.join(map(str, h['samples_ms']))) for t in traces for h in t['hops']],
              ['destination','status','ttl','ips','samples_ms','avg_ms','min_ms','unreachable','destination_response'])


def retrace(args):
    out = Path(args.output)
    meta = json.loads((out / 'metadata.json').read_text())
    config = argparse.Namespace(**meta['config'])
    config.output = args.output
    collect_traces(config, out, read_ips(out / 'input_ips.txt'), meta)
    render(config)


def correlation(xs, ys):
    import numpy as np
    if len(xs) < 3 or np.std(xs) == 0 or np.std(ys) == 0:
        return 'undefined (insufficient variation or fewer than 3 points)'
    return f'{np.corrcoef(xs, ys)[0, 1]:.3f}'


def render(args):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    import numpy as np
    out = Path(args.output)
    rows = json.loads((out / 'ping.json').read_text())
    traces = json.loads((out / 'traceroute.json').read_text())
    meta = json.loads((out / 'metadata.json').read_text())
    repo = args.repo_url or meta['config'].get('repo_url')
    if repo:
        meta['config']['repo_url'] = repo
        save_json(out / 'metadata.json', meta)
    selected = [t for t in traces if t['status'] == 'complete'][:5]
    valid = [r for r in rows if r['avg_ms'] is not None and r.get('distance_km') is not None]
    remote = [r for r in valid if not r['own_ip']]
    plots = out / 'plots'
    plots.mkdir(exist_ok=True)
    plt.rcParams.update({'font.size': 10, 'axes.spines.top': False,
                         'axes.spines.right': False, 'axes.unicode_minus': False})
    figures = []
    fig, ax = plt.subplots(figsize=(9, 5.5), layout='constrained')
    for key, label, marker in [('min_ms','Minimum','v'), ('max_ms','Maximum','^'), ('avg_ms','Mean','o')]:
        ax.scatter([r['distance_km'] for r in valid], [r[key] for r in valid], s=22, alpha=.65, label=label, marker=marker)
    own = [r for r in valid if r['own_ip']]
    if own:
        ax.scatter([0], [own[0]['avg_ms']], s=120, marker='*', color='black', label='Own public IP')
    if not valid:
        ax.text(.5,.5,'No responsive, geolocated destinations',transform=ax.transAxes,ha='center')
    ax.set(xlabel='Great-circle distance (km)', ylabel='Ping RTT (ms)', title='Distance versus round-trip time')
    ax.legend(); ax.grid(alpha=.2)
    figures.append(('distance_vs_rtt', fig))
    fig, ax = plt.subplots(figsize=(10, 6), layout='constrained')
    signed_rows = []
    for i, trace in enumerate(selected):
        previous, previous_ttl, positive, negative = 0.0, 0, 0.0, 0.0
        for hop in trace['hops']:
            if hop['avg_ms'] is None:
                continue
            delta = hop['avg_ms'] - previous
            bottom = positive if delta >= 0 else negative
            label = f'{previous_ttl}->{hop["ttl"]}'
            ax.bar(i, delta, bottom=bottom, color=plt.get_cmap('tab20')((hop['ttl']-1)%20),
                   edgecolor='white', linewidth=.4, hatch='//' if hop['ttl']-previous_ttl > 1 else None)
            if abs(delta) > 10:
                ax.text(i, bottom+delta/2, label, ha='center', va='center', fontsize=7)
            if delta >= 0: positive += delta
            else: negative += delta
            signed_rows.append(dict(ip=trace['ip'], from_ttl=previous_ttl, to_ttl=hop['ttl'],
                                    signed_delta_ms=delta, spans_missing_hops=hop['ttl']-previous_ttl > 1))
            previous, previous_ttl = hop['avg_ms'], hop['ttl']
        ax.scatter(i, trace['final_rtt_ms'], color='black', marker='D', zorder=5,
                   label='Destination mean RTT' if i == 0 else None)
    ax.axhline(0, color='black', linewidth=.7)
    ax.set_xticks(range(len(selected)), [t['ip'] for t in selected], rotation=20, ha='right')
    ax.set(ylabel='Signed change in cumulative mean RTT (ms)', title='Traceroute latency breakdown: adjacent responding TTLs')
    ax.text(.01, -.29, 'Segments labeled previous TTL -> current TTL. Hatched = spans unanswered hops.\n'
            'Negative changes retained; these are RTT differences, not measured link delays.', transform=ax.transAxes, fontsize=9)
    if selected: ax.legend()
    else: ax.text(.5,.5,'No completed traceroutes',transform=ax.transAxes,ha='center')
    figures.append(('hop_latency_breakdown', fig))
    write_csv(out / 'hop_deltas.csv', signed_rows, ['ip','from_ttl','to_ttl','signed_delta_ms','spans_missing_hops'])
    fig, ax = plt.subplots(figsize=(9, 5.5), layout='constrained')
    for trace in selected:
        ax.scatter(trace['hop_count'], trace['final_rtt_ms'], s=60)
        ax.annotate(trace['ip'], (trace['hop_count'], trace['final_rtt_ms']), xytext=(4,5), textcoords='offset points', fontsize=8)
    ax.set(xlabel='Destination TTL (hop count, including unanswered hops)', ylabel='Final-hop mean traceroute RTT (ms)', title='Hop count versus destination RTT')
    ax.xaxis.set_major_locator(matplotlib.ticker.MaxNLocator(integer=True)); ax.grid(alpha=.2)
    if not selected: ax.text(.5,.5,'No completed traceroutes',transform=ax.transAxes,ha='center')
    figures.append(('hop_count_vs_rtt', fig))
    for name, figure in figures:
        figure.savefig(plots / (name+'.pdf'), bbox_inches='tight')
        figure.savefig(plots / (name+'.png'), dpi=160, bbox_inches='tight')
    distance_r = correlation([r['distance_km'] for r in remote], [r['avg_ms'] for r in remote])
    hops_r = correlation([t['hop_count'] for t in selected], [t['final_rtt_ms'] for t in selected])
    sections = report_sections(meta, rows, traces, selected, valid, distance_r, hops_r, repo)
    md = ['# Assignment 1: Network Latencies, Ping & Traceroute', '']
    for title, paragraphs in sections:
        md.extend(['## '+title, '', '\n\n'.join(paragraphs), ''])
    for name, _ in figures:
        md.extend([f'![{name}](plots/{name}.png)', ''])
    md.extend(['## Full ping measurements', '', 'See `ping.csv` for all IPs, coordinates, min/mean/max RTT, loss and errors.', ''])
    (out / 'report.md').write_text('\n'.join(md))
    with PdfPages(out / 'report.pdf') as pdf:
        page_number = 1
        for title, paragraphs in sections:
            page_number = text_pages(pdf, title, paragraphs, plt, page_number)
        for _, figure in figures:
            page_number = save_pdf_page(pdf, figure, page_number)
        table_rows = []
        for row in rows:
            fmt = lambda value: '-' if value is None else f'{value:.2f}'
            table_rows.append([row['ip'], row['status'], fmt(row.get('latitude')), fmt(row.get('longitude')),
                               fmt(row.get('distance_km')),fmt(row['min_ms']),fmt(row['avg_ms']),fmt(row['max_ms'])])
        for offset in range(0, len(table_rows), 28):
            fig, ax = plt.subplots(figsize=(11.7, 8.3)); ax.axis('off')
            ax.set_title(f'Ping results and coordinates - {offset+1}-{min(offset+28,len(table_rows))}', loc='left', pad=20)
            table = ax.table(cellText=table_rows[offset:offset+28], colLabels=['IP','Status','Lat','Lon','km','Min ms','Mean ms','Max ms'],
                             loc='upper center', colWidths=[.29,.14,.09,.09,.1,.09,.1,.1])
            table.auto_set_font_size(False); table.set_fontsize(7); table.scale(1, 1.4)
            page_number = save_pdf_page(pdf, fig, page_number)
            plt.close(fig)
    for _, figure in figures: plt.close(figure)
    print(f'Created {out}/report.pdf, report.md, CSV data, and three PDF/PNG plots.', flush=True)
    if not repo: print('REMAINING: add your GitHub/GitLab URL with the render --repo-url option.', flush=True)
    if len(selected) < 5: print(f'REMAINING: only {len(selected)}/5 completed traceroutes; rerun from a network allowing replies.', flush=True)


def save_pdf_page(pdf, figure, page_number):
    figure.text(.5, .015, str(page_number), ha='center', va='bottom', fontsize=8, color='#555555')
    pdf.savefig(figure)
    return page_number + 1


def text_pages(pdf, title, paragraphs, plt, page_number):
    lines = []
    for p in paragraphs:
        lines.extend(textwrap.wrap(p, 96, break_long_words=True)); lines.append('')
    for start in range(0, max(1, len(lines)), 43):
        fig = plt.figure(figsize=(8.27,11.69))
        fig.text(.08,.94,title + (' (continued)' if start else ''), fontsize=17, weight='bold', va='top')
        fig.text(.08,.885,'\n'.join(lines[start:start+43]),fontsize=10,va='top',linespacing=1.5)
        page_number = save_pdf_page(pdf, fig, page_number)
        plt.close(fig)
    return page_number


def report_sections(meta, rows, traces, selected, valid, distance_r, hops_r, repo):
    cfg = meta['config']
    origin = meta['origin']
    responsive = sum(r['avg_ms'] is not None for r in rows)
    issues = []
    if not repo: issues.append('REQUIRED BEFORE SUBMISSION: replace the missing GitHub/GitLab repository URL.')
    if len(selected) < 5: issues.append(f'Only {len(selected)} of 5 required traceroutes completed; additional measurements are needed.')
    own = next(r for r in rows if r['own_ip'])
    files = ['fetch','ping_ip','geolocate','haversine','trace_ip','parse_trace','render']
    source_lines = (ROOT / 'experiment.py').read_text().splitlines()
    links = []
    for name in files:
        line = next(i for i, text in enumerate(source_lines,1) if text.startswith('def '+name+'('))
        links.append(f'experiment.py:{line} ({name})')
    trace_summary = '; '.join(f'{t["ip"]}: {t["hop_count"]} hops, {t["final_rtt_ms"]:.2f} ms' for t in selected) or 'No completed paths.'
    deltas = [b['avg_ms']-a['avg_ms'] for t in selected for responding in [[h for h in t['hops'] if h['avg_ms'] is not None]] for a,b in zip(responding,responding[1:])]
    remote = [r for r in valid if not r['own_ip']]
    if remote:
        nearest, farthest = min(remote, key=lambda r: r['distance_km']), max(remote, key=lambda r: r['distance_km'])
        widest = max(remote, key=lambda r: r['max_ms'] - r['min_ms'])
        measured_distance = (f"The nearest geolocated responding server was {nearest['ip']} at "
            f"{nearest['distance_km']:.0f} km with {nearest['avg_ms']:.2f} ms mean RTT; the farthest was "
            f"{farthest['ip']} at {farthest['distance_km']:.0f} km with {farthest['avg_ms']:.2f} ms. "
            f"The largest observed min-to-max spread was {widest['max_ms']-widest['min_ms']:.2f} ms "
            f"at {widest['ip']} ({widest['min_ms']:.2f} to {widest['max_ms']:.2f} ms).")
    else:
        measured_distance = 'No remote destinations had both coordinates and responsive pings; distance trends cannot be assessed.'
    if selected:
        most_hops, slowest = max(selected, key=lambda t: t['hop_count']), max(selected, key=lambda t: t['final_rtt_ms'])
        measured_hops = (f"The largest destination TTL was {most_hops['hop_count']} for {most_hops['ip']}, "
            f"with {most_hops['final_rtt_ms']:.2f} ms RTT. The highest RTT was {slowest['final_rtt_ms']:.2f} ms "
            f"for {slowest['ip']} at {slowest['hop_count']} hops. "
            + ('These are different paths: more hops did not imply the highest RTT in this sample.'
               if most_hops['ip'] != slowest['ip'] else
               'This path has both the largest TTL and highest RTT in this sample; that coincidence alone does not establish a general relationship.'))
    else:
        measured_hops = 'No complete paths were available for empirical hop-count comparisons.'
    return [
        ('Assignment 1: Network Latencies, Ping & Traceroute', [
            'Bhavin Gupta | CS 422: Computer Networks | Fall 2026',
            'Repository: '+(repo or 'MISSING - add the GitHub/GitLab URL before submission.'),
            ' '.join(issues) if issues else 'The experiment completed the full ping list and five traceroutes.',
            f'Measurement location: {origin.get("city", "unspecified")}. Public IP: {meta["own_ip"]}. Coordinates used for distance: {origin["latitude"]:.5f}, {origin["longitude"]:.5f}.',
            f'I used the server list from {SOURCE}, sent {cfg["count"]} pings per IP, and used {cfg["probes"]} probes per traceroute hop. Seed {cfg["seed"]} controls the random traceroute selection.',
            f'Rerun command: ./run.sh --origin-label "{origin.get("city", "measurement location")}" --origin-lat {origin["latitude"]} --origin-lon {origin["longitude"]} --repo-url {repo or "YOUR_REPOSITORY_URL"}',
            'Code sections: '+'; '.join(links)+'. Raw command output is in results/raw.']),
        ('1. Ping results and distance', [
            f'I tested {len(rows)} unique IP addresses, including my public IP. {responsive} responded and {len(rows)-responsive} did not. The plot contains the {len(valid)} addresses with both RTT and coordinates. Missing replies are not treated as zero.',
            f'My public IP averaged {own["avg_ms"]} ms and is shown at zero distance as a local reference.',
            f'The plot compares great-circle distance with minimum, average, and maximum RTT. The Pearson correlation between distance and average RTT was {distance_r}, excluding my own IP.',
            measured_distance,
            'RTT usually increased with distance, but distance was not the only factor. Internet routes are not straight lines, and processing, link speed, and queueing also add delay.',
            'Minimum RTT is the best baseline seen during the test. Maximum RTT includes the worst delay seen. A large min-max gap shows that network conditions changed between packets.',
            'Coordinates came from ipwho.is and are approximate. The full data is in the appendix and results/ping.csv.']),
        ('2. Traceroute results and hop count', [
            f'I shuffled the IP list with seed {cfg["seed"]} and tried addresses until five destinations responded. {len(traces)} paths were attempted and {len(selected)} completed.',
            trace_summary,
            'Asterisks are unanswered hops. A trace only counts when the final destination responds. Hop count is the final TTL, including unanswered intermediate hops.',
            'The stacked chart subtracts each responding hop RTT from the next responding hop RTT. Hatched bars cross unanswered hops. The black diamond shows the destination RTT.',
            f'There were {sum(d < 0 for d in deltas)} negative differences. Each traceroute probe is a different packet, so queueing and return paths can vary. A negative bar is measurement variation, not negative link delay.',
            f'The correlation between hop count and final RTT was {hops_r}. There are only five paths, so this result only describes this run.',
            measured_hops,
            'More hops can add delay, but hop count alone does not predict RTT. A few long links can take more time than many short links. Route length and queueing also matter.'])]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('fetch', help='Snapshot server list and resolve every A/AAAA address')
    p.set_defaults(func=fetch)
    p = sub.add_parser('run', help='Collect real measurements and generate all output')
    p.add_argument('--input', default='inputs/ips.txt')
    p.add_argument('--output', default='results')
    p.add_argument('--count', type=int, default=5)
    p.add_argument('--wait', type=float, default=1.0)
    p.add_argument('--workers', type=int, default=8)
    p.add_argument('--trace-workers', type=int, default=5)
    p.add_argument('--max-hops', type=int, default=30)
    p.add_argument('--probes', type=int, default=3)
    p.add_argument('--trace-timeout', type=float, default=120)
    p.add_argument('--seed', type=int, default=422)
    p.add_argument('--origin-label', help='Human-readable measurement location')
    p.add_argument('--origin-lat', type=float)
    p.add_argument('--origin-lon', type=float)
    p.add_argument('--own-ip')
    p.add_argument('--repo-url')
    p.set_defaults(func=run)
    p = sub.add_parser('retrace', help='Repeat traceroutes and render using saved ping data and settings')
    p.add_argument('--output', default='results')
    p.set_defaults(func=retrace)
    p = sub.add_parser('render', help='Regenerate plots/report offline from saved results')
    p.add_argument('--output', default='results')
    p.add_argument('--repo-url')
    p.set_defaults(func=render)
    args = parser.parse_args()
    if args.action == 'run':
        for key in ['count','wait','workers','trace_workers','max_hops','probes','trace_timeout']:
            if getattr(args, key) <= 0: parser.error('--'+key.replace('_','-')+' must be positive')
        if (args.origin_lat is None) != (args.origin_lon is None):
            parser.error('Supply both --origin-lat and --origin-lon')
    try:
        args.func(args)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'Error: {exc}\n')


if __name__ == '__main__':
    main()

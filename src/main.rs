use std::collections::HashMap;
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{self, BufRead, BufReader, Read, Write};
use std::net::{
    IpAddr, Ipv4Addr, Ipv6Addr, Shutdown, SocketAddr, TcpListener, TcpStream, ToSocketAddrs,
    UdpSocket,
};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const DEFAULT_CONFIG_PATH: &str = "/etc/v2ray-egress-sidecar/config.toml";
const DEFAULT_RELOAD_INTERVAL_SECS: u64 = 5;
const DEFAULT_THREAD_STACK_BYTES: usize = 256 * 1024;
const DEFAULT_UDP_IDLE_TIMEOUT_SECS: u64 = 60;
const DEFAULT_TCP_IDLE_TIMEOUT_SECS: u64 = 300;
const DEFAULT_TCP_HALF_CLOSE_TIMEOUT_SECS: u64 = 10;

#[derive(Clone, Debug)]
struct Config {
    listen: String,
    stats_path: String,
    reload_interval_secs: u64,
    max_connections: usize,
    thread_stack_bytes: usize,
    tcp_idle_timeout_secs: u64,
    tcp_half_close_timeout_secs: u64,
    udp_idle_timeout_secs: u64,
    outbounds: Vec<OutboundConfig>,
    rules: Vec<Rule>,
}

#[derive(Clone, Debug)]
struct OutboundConfig {
    tag: String,
    kind: OutboundKind,
    addr: Option<String>,
    username: Option<String>,
    password: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum OutboundKind {
    Direct,
    Socks5,
    HttpConnect,
}

#[derive(Clone, Debug)]
struct Rule {
    rule_type: RuleType,
    value: String,
    outbound: String,
}

#[derive(Clone, Debug)]
enum RuleType {
    DomainExact,
    DomainSuffix,
    DomainContains,
}

#[derive(Clone)]
struct RuntimeSnapshot {
    config: Config,
    outbounds: HashMap<String, OutboundConfig>,
}

struct RuntimeState {
    config: Config,
    outbounds: HashMap<String, OutboundConfig>,
    modified: SystemTime,
    failed_modified: Option<SystemTime>,
}

struct AppState {
    config_path: PathBuf,
    runtime: Arc<Mutex<RuntimeState>>,
    stats_lock: Mutex<()>,
    active_connections: AtomicUsize,
}

#[derive(Default)]
struct ConnStats {
    network: String,
    domain: String,
    port: u16,
    outbound: String,
    uplink_bytes: u64,
    downlink_bytes: u64,
    elapsed_ms: u128,
    status: String,
    error: Option<String>,
}

enum Socks5Request {
    Connect { host: String, port: u16 },
    UdpAssociate,
}

struct UdpPacket<'a> {
    host: String,
    port: u16,
    payload: &'a [u8],
}

fn main() {
    if let Err(err) = run() {
        eprintln!("fatal: {}", err);
        std::process::exit(1);
    }
}

fn run() -> io::Result<()> {
    let config_path = parse_config_path();
    let runtime = load_runtime(&config_path)?;
    let listen_addr = runtime.config.listen.clone();
    ensure_stats_dir(&runtime.config.stats_path)?;

    let state = Arc::new(AppState {
        config_path,
        runtime: Arc::new(Mutex::new(runtime)),
        stats_lock: Mutex::new(()),
        active_connections: AtomicUsize::new(0),
    });

    spawn_reload_thread(Arc::clone(&state));

    let listener = TcpListener::bind(&listen_addr)?;
    eprintln!("v2ray-egress-sidecar listening on {}", listen_addr);

    for accepted in listener.incoming() {
        let stream = match accepted {
            Ok(stream) => stream,
            Err(err) => {
                eprintln!("accept error: {}", err);
                continue;
            }
        };

        let snapshot = state.snapshot();
        let active = state.active_connections.fetch_add(1, Ordering::SeqCst) + 1;
        if snapshot.config.max_connections > 0 && active > snapshot.config.max_connections {
            state.active_connections.fetch_sub(1, Ordering::SeqCst);
            let _ = stream.shutdown(Shutdown::Both);
            eprintln!(
                "connection rejected: active={} max_connections={}",
                active, snapshot.config.max_connections
            );
            continue;
        }

        let worker_state = Arc::clone(&state);
        let stack_size = snapshot.config.thread_stack_bytes;
        let spawn_result = spawn_with_stack("sidecar-client", stack_size, move || {
            let _guard = ActiveConnectionGuard {
                state: Arc::clone(&worker_state),
            };
            if let Err(err) = handle_client(stream, worker_state) {
                eprintln!("client error: {}", err);
            }
        });

        if let Err(err) = spawn_result {
            state.active_connections.fetch_sub(1, Ordering::SeqCst);
            eprintln!("failed to spawn client worker: {}", err);
        }
    }

    Ok(())
}

struct ActiveConnectionGuard {
    state: Arc<AppState>,
}

impl Drop for ActiveConnectionGuard {
    fn drop(&mut self) {
        self.state.active_connections.fetch_sub(1, Ordering::SeqCst);
    }
}

impl AppState {
    fn snapshot(&self) -> RuntimeSnapshot {
        let runtime = self.runtime.lock().unwrap();
        RuntimeSnapshot {
            config: runtime.config.clone(),
            outbounds: runtime.outbounds.clone(),
        }
    }

    fn reload_interval_secs(&self) -> u64 {
        let runtime = self.runtime.lock().unwrap();
        runtime.config.reload_interval_secs.max(1)
    }

    fn reload_if_changed(&self) {
        let modified = match config_modified(&self.config_path) {
            Ok(modified) => modified,
            Err(err) => {
                eprintln!("config mtime check failed: {}", err);
                return;
            }
        };

        {
            let runtime = self.runtime.lock().unwrap();
            if modified <= runtime.modified || runtime.failed_modified == Some(modified) {
                return;
            }
        }

        match load_runtime(&self.config_path) {
            Ok(new_runtime) => {
                if let Err(err) = ensure_stats_dir(&new_runtime.config.stats_path) {
                    eprintln!("config reload rejected: stats dir error: {}", err);
                    let mut runtime = self.runtime.lock().unwrap();
                    runtime.failed_modified = Some(modified);
                    return;
                }

                let mut runtime = self.runtime.lock().unwrap();
                if new_runtime.config.listen != runtime.config.listen {
                    eprintln!(
                        "config reload note: listen changed from {} to {}; restart service to change listener",
                        runtime.config.listen, new_runtime.config.listen
                    );
                }
                runtime.config = new_runtime.config;
                runtime.outbounds = new_runtime.outbounds;
                runtime.modified = modified;
                runtime.failed_modified = None;
                eprintln!("config reloaded successfully");
            }
            Err(err) => {
                eprintln!("config reload rejected: {}", err);
                let mut runtime = self.runtime.lock().unwrap();
                runtime.failed_modified = Some(modified);
            }
        }
    }
}

fn spawn_reload_thread(state: Arc<AppState>) {
    let _ = spawn_with_stack("sidecar-reload", DEFAULT_THREAD_STACK_BYTES, move || loop {
        let interval = state.reload_interval_secs();
        thread::sleep(Duration::from_secs(interval));
        state.reload_if_changed();
    });
}

fn handle_client(mut client: TcpStream, state: Arc<AppState>) -> io::Result<()> {
    client.set_nodelay(true).ok();
    let started = Instant::now();
    let request = socks5_accept(&mut client)?;
    let (target_host, target_port) = match request {
        Socks5Request::Connect { host, port } => (host, port),
        Socks5Request::UdpAssociate => return handle_udp_associate(client, state, started),
    };
    let snapshot = state.snapshot();
    let outbound_tag = choose_outbound(&snapshot.config, &target_host);
    let outbound = snapshot
        .outbounds
        .get(&outbound_tag)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "missing outbound"))?
        .clone();

    let mut stats = ConnStats {
        network: "tcp".to_string(),
        domain: target_host.clone(),
        port: target_port,
        outbound: outbound_tag,
        status: "ok".to_string(),
        ..ConnStats::default()
    };

    let remote_result = connect_outbound(&outbound, &target_host, target_port);
    let remote = match remote_result {
        Ok(remote) => {
            socks5_reply_success(&mut client)?;
            remote
        }
        Err(err) => {
            stats.status = "connect_error".to_string();
            stats.error = Some(err.to_string());
            stats.elapsed_ms = started.elapsed().as_millis();
            let _ = socks5_reply_failure(&mut client);
            state.write_stats(&stats);
            return Err(err);
        }
    };

    remote.set_nodelay(true).ok();
    match copy_bidirectional(
        client,
        remote,
        snapshot.config.thread_stack_bytes,
        snapshot.config.tcp_idle_timeout_secs,
        snapshot.config.tcp_half_close_timeout_secs,
    ) {
        Ok((uplink, downlink)) => {
            stats.uplink_bytes = uplink;
            stats.downlink_bytes = downlink;
        }
        Err(err) => {
            stats.status = "proxy_error".to_string();
            stats.error = Some(err.to_string());
        }
    }
    stats.elapsed_ms = started.elapsed().as_millis();
    state.write_stats(&stats);
    Ok(())
}

impl AppState {
    fn write_stats(&self, stats: &ConnStats) {
        let snapshot = self.snapshot();
        let _guard = self.stats_lock.lock().unwrap();
        if let Err(err) = append_stats(&snapshot.config.stats_path, stats) {
            eprintln!("stats write failed: {}", err);
        }
    }
}

fn handle_udp_associate(
    mut client: TcpStream,
    state: Arc<AppState>,
    started: Instant,
) -> io::Result<()> {
    let idle_timeout_secs = state.snapshot().config.udp_idle_timeout_secs;
    let idle_timeout = if idle_timeout_secs > 0 {
        Some(Duration::from_secs(idle_timeout_secs))
    } else {
        None
    };
    let client_udp = UdpSocket::bind(udp_bind_addr_for(&client)?)?;
    client_udp.set_read_timeout(Some(Duration::from_millis(20)))?;

    let upstream_v4 = UdpSocket::bind(SocketAddr::from((Ipv4Addr::UNSPECIFIED, 0)))?;
    upstream_v4.set_nonblocking(true)?;
    let upstream_v6 = UdpSocket::bind(SocketAddr::from((Ipv6Addr::UNSPECIFIED, 0))).ok();
    if let Some(socket) = &upstream_v6 {
        socket.set_nonblocking(true)?;
    }

    socks5_reply_success_bound(&mut client, client_udp.local_addr()?)?;
    client.set_nonblocking(true).ok();

    let mut client_peer: Option<SocketAddr> = None;
    let mut client_buf = vec![0u8; 65_535];
    let mut upstream_buf = vec![0u8; 65_535];
    let mut target_host = String::from("(udp)");
    let mut target_port = 0u16;
    let mut outbound_tag = String::from("direct");
    let mut uplink_bytes = 0u64;
    let mut downlink_bytes = 0u64;
    let mut errors = 0u64;
    let mut last_error: Option<String> = None;
    let mut last_activity = Instant::now();

    loop {
        if let Some(timeout) = idle_timeout {
            if last_activity.elapsed() >= timeout {
                break;
            }
        }
        if socks_control_closed(&client) {
            break;
        }

        match client_udp.recv_from(&mut client_buf) {
            Ok((nread, source)) => {
                if client_peer.is_none() {
                    client_peer = Some(source);
                }
                if client_peer != Some(source) {
                    continue;
                }
                last_activity = Instant::now();

                match parse_socks5_udp_packet(&client_buf[..nread]) {
                    Ok(packet) => {
                        target_host = packet.host.clone();
                        target_port = packet.port;

                        let snapshot = state.snapshot();
                        let tag = choose_outbound(&snapshot.config, &packet.host);
                        outbound_tag = tag.clone();
                        let Some(outbound) = snapshot.outbounds.get(&tag) else {
                            errors += 1;
                            last_error = Some(format!("missing outbound {}", tag));
                            continue;
                        };

                        if outbound.kind != OutboundKind::Direct {
                            errors += 1;
                            last_error = Some(format!(
                                "UDP outbound {} via {:?} is not supported",
                                tag, outbound.kind
                            ));
                            continue;
                        }

                        let target = match resolve_udp_target(&packet.host, packet.port) {
                            Ok(target) => target,
                            Err(err) => {
                                errors += 1;
                                last_error = Some(err.to_string());
                                continue;
                            }
                        };

                        let sent = if target.is_ipv4() {
                            upstream_v4.send_to(packet.payload, target)
                        } else if let Some(socket) = &upstream_v6 {
                            socket.send_to(packet.payload, target)
                        } else {
                            Err(io::Error::new(
                                io::ErrorKind::AddrNotAvailable,
                                "IPv6 UDP is not available",
                            ))
                        };

                        match sent {
                            Ok(bytes) => uplink_bytes += bytes as u64,
                            Err(err) => {
                                errors += 1;
                                last_error = Some(err.to_string());
                            }
                        }
                    }
                    Err(err) => {
                        errors += 1;
                        last_error = Some(err.to_string());
                    }
                }
            }
            Err(err)
                if err.kind() == io::ErrorKind::WouldBlock
                    || err.kind() == io::ErrorKind::TimedOut => {}
            Err(err) if err.kind() == io::ErrorKind::Interrupted => continue,
            Err(err) => return Err(err),
        }

        if let Some(peer) = client_peer {
            drain_upstream_udp(
                &client_udp,
                &upstream_v4,
                peer,
                &mut upstream_buf,
                &mut downlink_bytes,
                &mut last_activity,
            )?;
            if let Some(socket) = &upstream_v6 {
                drain_upstream_udp(
                    &client_udp,
                    socket,
                    peer,
                    &mut upstream_buf,
                    &mut downlink_bytes,
                    &mut last_activity,
                )?;
            }
        }
    }

    if uplink_bytes > 0 || downlink_bytes > 0 || errors > 0 {
        let failed = uplink_bytes == 0 && downlink_bytes == 0 && errors > 0;
        let stats = ConnStats {
            network: "udp".to_string(),
            domain: target_host,
            port: target_port,
            outbound: outbound_tag,
            uplink_bytes,
            downlink_bytes,
            elapsed_ms: started.elapsed().as_millis(),
            status: if failed { "udp_error" } else { "ok" }.to_string(),
            error: if failed { last_error } else { None },
        };
        state.write_stats(&stats);
    }

    Ok(())
}

fn udp_bind_addr_for(client: &TcpStream) -> io::Result<SocketAddr> {
    let ip = match client.local_addr()?.ip() {
        IpAddr::V4(addr) if addr.is_unspecified() => IpAddr::V4(Ipv4Addr::LOCALHOST),
        IpAddr::V6(addr) if addr.is_unspecified() => IpAddr::V6(Ipv6Addr::LOCALHOST),
        addr => addr,
    };
    Ok(SocketAddr::new(ip, 0))
}

fn socks_control_closed(client: &TcpStream) -> bool {
    let mut buf = [0u8; 1];
    match client.peek(&mut buf) {
        Ok(0) => true,
        Ok(_) => false,
        Err(err)
            if err.kind() == io::ErrorKind::WouldBlock
                || err.kind() == io::ErrorKind::TimedOut
                || err.kind() == io::ErrorKind::Interrupted =>
        {
            false
        }
        Err(_) => true,
    }
}

fn parse_socks5_udp_packet(packet: &[u8]) -> io::Result<UdpPacket<'_>> {
    if packet.len() < 4 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "short UDP packet",
        ));
    }
    if packet[0] != 0 || packet[1] != 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "bad UDP reserved bytes",
        ));
    }
    if packet[2] != 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "fragmented SOCKS5 UDP is not supported",
        ));
    }

    let mut pos = 4;
    let host = match packet[3] {
        1 => {
            if packet.len() < pos + 4 + 2 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "short IPv4 UDP packet",
                ));
            }
            let octets = [
                packet[pos],
                packet[pos + 1],
                packet[pos + 2],
                packet[pos + 3],
            ];
            pos += 4;
            IpAddr::from(octets).to_string()
        }
        3 => {
            if packet.len() < pos + 1 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "short domain UDP packet",
                ));
            }
            let len = packet[pos] as usize;
            pos += 1;
            if packet.len() < pos + len + 2 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "short domain UDP packet",
                ));
            }
            let host = String::from_utf8(packet[pos..pos + len].to_vec())
                .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid UDP domain"))?;
            pos += len;
            host
        }
        4 => {
            if packet.len() < pos + 16 + 2 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "short IPv6 UDP packet",
                ));
            }
            let mut octets = [0u8; 16];
            octets.copy_from_slice(&packet[pos..pos + 16]);
            pos += 16;
            IpAddr::from(octets).to_string()
        }
        _ => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "unsupported UDP address type",
            ))
        }
    };

    let port = u16::from_be_bytes([packet[pos], packet[pos + 1]]);
    pos += 2;
    Ok(UdpPacket {
        host,
        port,
        payload: &packet[pos..],
    })
}

fn resolve_udp_target(host: &str, port: u16) -> io::Result<SocketAddr> {
    if let Ok(ip) = host.parse::<IpAddr>() {
        return Ok(SocketAddr::new(ip, port));
    }
    (host, port)
        .to_socket_addrs()?
        .next()
        .ok_or_else(|| io::Error::new(io::ErrorKind::AddrNotAvailable, "UDP target not found"))
}

fn drain_upstream_udp(
    client_udp: &UdpSocket,
    upstream: &UdpSocket,
    client_peer: SocketAddr,
    buf: &mut [u8],
    downlink_bytes: &mut u64,
    last_activity: &mut Instant,
) -> io::Result<()> {
    loop {
        match upstream.recv_from(buf) {
            Ok((nread, source)) => {
                *last_activity = Instant::now();
                let mut response = Vec::with_capacity(22 + nread);
                append_socks5_udp_header(&mut response, source);
                response.extend_from_slice(&buf[..nread]);
                client_udp.send_to(&response, client_peer)?;
                *downlink_bytes += nread as u64;
            }
            Err(err) if err.kind() == io::ErrorKind::WouldBlock => return Ok(()),
            Err(err) if err.kind() == io::ErrorKind::Interrupted => continue,
            Err(err) => return Err(err),
        }
    }
}

fn append_socks5_udp_header(out: &mut Vec<u8>, addr: SocketAddr) {
    out.extend_from_slice(&[0, 0, 0]);
    match addr.ip() {
        IpAddr::V4(ip) => {
            out.push(1);
            out.extend_from_slice(&ip.octets());
        }
        IpAddr::V6(ip) => {
            out.push(4);
            out.extend_from_slice(&ip.octets());
        }
    }
    out.extend_from_slice(&addr.port().to_be_bytes());
}

fn copy_bidirectional(
    mut client: TcpStream,
    mut remote: TcpStream,
    stack_size: usize,
    idle_timeout_secs: u64,
    half_close_timeout_secs: u64,
) -> io::Result<(u64, u64)> {
    let mut client_reader = client.try_clone()?;
    let mut remote_writer = remote.try_clone()?;
    let idle_timeout = if idle_timeout_secs > 0 {
        Some(Duration::from_secs(idle_timeout_secs))
    } else {
        None
    };
    let half_close_timeout = if half_close_timeout_secs > 0 {
        Some(Duration::from_secs(half_close_timeout_secs))
    } else {
        None
    };
    if idle_timeout.is_some() || half_close_timeout.is_some() {
        client_reader
            .set_read_timeout(Some(Duration::from_secs(1)))
            .ok();
        remote.set_read_timeout(Some(Duration::from_secs(1))).ok();
    }
    let last_activity = Arc::new(AtomicU64::new(now_unix_ms()));
    let upload_activity = Arc::clone(&last_activity);
    let upload_done_at = Arc::new(AtomicU64::new(0));
    let upload_done_for_thread = Arc::clone(&upload_done_at);

    let upload = spawn_with_stack("sidecar-copy-up", stack_size, move || -> io::Result<u64> {
        let copied = copy_with_idle(
            &mut client_reader,
            &mut remote_writer,
            upload_activity,
            idle_timeout,
            None,
            None,
        )?;
        upload_done_for_thread.store(now_unix_ms(), Ordering::Relaxed);
        let _ = remote_writer.shutdown(Shutdown::Write);
        Ok(copied)
    })?;

    let downlink = copy_with_idle(
        &mut remote,
        &mut client,
        last_activity,
        idle_timeout,
        Some(upload_done_at),
        half_close_timeout,
    )?;
    let _ = client.shutdown(Shutdown::Write);
    let _ = client.shutdown(Shutdown::Read);

    let uplink = match upload.join() {
        Ok(result) => result?,
        Err(_) => return Err(io::Error::new(io::ErrorKind::Other, "copy thread panicked")),
    };
    Ok((uplink, downlink))
}

fn copy_with_idle(
    reader: &mut TcpStream,
    writer: &mut TcpStream,
    last_activity: Arc<AtomicU64>,
    idle_timeout: Option<Duration>,
    peer_done_at: Option<Arc<AtomicU64>>,
    peer_done_timeout: Option<Duration>,
) -> io::Result<u64> {
    let mut copied = 0u64;
    let mut buf = [0u8; 16 * 1024];
    loop {
        match reader.read(&mut buf) {
            Ok(0) => return Ok(copied),
            Ok(nread) => {
                last_activity.store(now_unix_ms(), Ordering::Relaxed);
                writer.write_all(&buf[..nread])?;
                copied += nread as u64;
            }
            Err(err)
                if err.kind() == io::ErrorKind::TimedOut
                    || err.kind() == io::ErrorKind::WouldBlock =>
            {
                let now = now_unix_ms();
                if let (Some(done_at), Some(timeout)) = (&peer_done_at, peer_done_timeout) {
                    let done_ms = done_at.load(Ordering::Relaxed);
                    if done_ms > 0 && now.saturating_sub(done_ms) >= timeout.as_millis() as u64 {
                        return Ok(copied);
                    }
                }
                if let Some(timeout) = idle_timeout {
                    let idle_ms = now.saturating_sub(last_activity.load(Ordering::Relaxed));
                    if idle_ms >= timeout.as_millis() as u64 {
                        return Ok(copied);
                    }
                }
            }
            Err(err) if err.kind() == io::ErrorKind::Interrupted => continue,
            Err(err) => return Err(err),
        }
    }
}

fn now_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_else(|_| Duration::from_secs(0))
        .as_millis() as u64
}

fn spawn_with_stack<F, T>(name: &str, stack_size: usize, f: F) -> io::Result<thread::JoinHandle<T>>
where
    F: FnOnce() -> T + Send + 'static,
    T: Send + 'static,
{
    thread::Builder::new()
        .name(name.to_string())
        .stack_size(stack_size.max(64 * 1024))
        .spawn(f)
}

fn socks5_accept(stream: &mut TcpStream) -> io::Result<Socks5Request> {
    let mut header = [0u8; 2];
    stream.read_exact(&mut header)?;
    if header[0] != 5 {
        return Err(io::Error::new(io::ErrorKind::InvalidData, "not SOCKS5"));
    }
    let mut methods = vec![0u8; header[1] as usize];
    stream.read_exact(&mut methods)?;
    if !methods.contains(&0) {
        stream.write_all(&[5, 0xff])?;
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "no supported auth method",
        ));
    }
    stream.write_all(&[5, 0])?;

    let mut req = [0u8; 4];
    stream.read_exact(&mut req)?;
    if req[0] != 5 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "bad SOCKS5 request",
        ));
    }

    let host = match req[3] {
        1 => {
            let mut buf = [0u8; 4];
            stream.read_exact(&mut buf)?;
            IpAddr::from(buf).to_string()
        }
        3 => {
            let mut len = [0u8; 1];
            stream.read_exact(&mut len)?;
            let mut buf = vec![0u8; len[0] as usize];
            stream.read_exact(&mut buf)?;
            String::from_utf8(buf)
                .map_err(|_| io::Error::new(io::ErrorKind::InvalidData, "invalid domain"))?
        }
        4 => {
            let mut buf = [0u8; 16];
            stream.read_exact(&mut buf)?;
            IpAddr::from(buf).to_string()
        }
        _ => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "unsupported address type",
            ))
        }
    };
    let mut port = [0u8; 2];
    stream.read_exact(&mut port)?;
    let port = u16::from_be_bytes(port);

    match req[1] {
        1 => Ok(Socks5Request::Connect { host, port }),
        3 => Ok(Socks5Request::UdpAssociate),
        cmd => {
            let _ = socks5_reply_command_not_supported(stream);
            Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("unsupported SOCKS5 command {}", cmd),
            ))
        }
    }
}

fn socks5_reply_success(stream: &mut TcpStream) -> io::Result<()> {
    socks5_reply_success_bound(stream, SocketAddr::from((Ipv4Addr::UNSPECIFIED, 0)))
}

fn socks5_reply_failure(stream: &mut TcpStream) -> io::Result<()> {
    socks5_reply_with_code(stream, 5, SocketAddr::from((Ipv4Addr::UNSPECIFIED, 0)))
}

fn socks5_reply_command_not_supported(stream: &mut TcpStream) -> io::Result<()> {
    socks5_reply_with_code(stream, 7, SocketAddr::from((Ipv4Addr::UNSPECIFIED, 0)))
}

fn socks5_reply_success_bound(stream: &mut TcpStream, bound: SocketAddr) -> io::Result<()> {
    socks5_reply_with_code(stream, 0, bound)
}

fn socks5_reply_with_code(stream: &mut TcpStream, code: u8, bound: SocketAddr) -> io::Result<()> {
    let mut reply = vec![5, code, 0];
    match bound.ip() {
        IpAddr::V4(ip) => {
            reply.push(1);
            reply.extend_from_slice(&ip.octets());
        }
        IpAddr::V6(ip) => {
            reply.push(4);
            reply.extend_from_slice(&ip.octets());
        }
    }
    reply.extend_from_slice(&bound.port().to_be_bytes());
    stream.write_all(&reply)
}

fn connect_outbound(outbound: &OutboundConfig, host: &str, port: u16) -> io::Result<TcpStream> {
    match outbound.kind {
        OutboundKind::Direct => TcpStream::connect((host, port)),
        OutboundKind::Socks5 => connect_via_socks5(outbound, host, port),
        OutboundKind::HttpConnect => connect_via_http_connect(outbound, host, port),
    }
}

fn connect_via_socks5(outbound: &OutboundConfig, host: &str, port: u16) -> io::Result<TcpStream> {
    let proxy = outbound.addr.as_ref().ok_or_else(|| {
        io::Error::new(io::ErrorKind::InvalidInput, "socks5 outbound missing addr")
    })?;
    let mut stream = TcpStream::connect(proxy)?;
    stream.set_nodelay(true).ok();

    if outbound.username.is_some() || outbound.password.is_some() {
        stream.write_all(&[5, 2, 0, 2])?;
        let mut resp = [0u8; 2];
        stream.read_exact(&mut resp)?;
        if resp == [5, 2] {
            let username = outbound.username.as_deref().unwrap_or("");
            let password = outbound.password.as_deref().unwrap_or("");
            if username.len() > 255 || password.len() > 255 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "SOCKS5 credentials too long",
                ));
            }
            let mut auth = Vec::with_capacity(3 + username.len() + password.len());
            auth.push(1);
            auth.push(username.len() as u8);
            auth.extend_from_slice(username.as_bytes());
            auth.push(password.len() as u8);
            auth.extend_from_slice(password.as_bytes());
            stream.write_all(&auth)?;
            let mut auth_resp = [0u8; 2];
            stream.read_exact(&mut auth_resp)?;
            if auth_resp != [1, 0] {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "SOCKS5 auth failed",
                ));
            }
        } else if resp != [5, 0] {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "SOCKS5 method rejected",
            ));
        }
    } else {
        stream.write_all(&[5, 1, 0])?;
        let mut resp = [0u8; 2];
        stream.read_exact(&mut resp)?;
        if resp != [5, 0] {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "SOCKS5 no-auth rejected",
            ));
        }
    }

    write_socks5_connect_request(&mut stream, host, port)?;
    read_socks5_connect_response(&mut stream)?;
    Ok(stream)
}

fn write_socks5_connect_request(stream: &mut TcpStream, host: &str, port: u16) -> io::Result<()> {
    let mut request = vec![5, 1, 0];
    if let Ok(ip) = host.parse::<IpAddr>() {
        match ip {
            IpAddr::V4(ipv4) => {
                request.push(1);
                request.extend_from_slice(&ipv4.octets());
            }
            IpAddr::V6(ipv6) => {
                request.push(4);
                request.extend_from_slice(&ipv6.octets());
            }
        }
    } else {
        if host.len() > 255 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "domain too long",
            ));
        }
        request.push(3);
        request.push(host.len() as u8);
        request.extend_from_slice(host.as_bytes());
    }
    request.extend_from_slice(&port.to_be_bytes());
    stream.write_all(&request)
}

fn read_socks5_connect_response(stream: &mut TcpStream) -> io::Result<()> {
    let mut resp = [0u8; 4];
    stream.read_exact(&mut resp)?;
    if resp[1] != 0 {
        return Err(io::Error::new(
            io::ErrorKind::Other,
            format!("SOCKS5 proxy connect failed: {}", resp[1]),
        ));
    }
    match resp[3] {
        1 => {
            let mut buf = [0u8; 4];
            stream.read_exact(&mut buf)?;
        }
        3 => {
            let mut len = [0u8; 1];
            stream.read_exact(&mut len)?;
            let mut buf = vec![0u8; len[0] as usize];
            stream.read_exact(&mut buf)?;
        }
        4 => {
            let mut buf = [0u8; 16];
            stream.read_exact(&mut buf)?;
        }
        _ => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "bad SOCKS5 response atyp",
            ))
        }
    }
    let mut port = [0u8; 2];
    stream.read_exact(&mut port)?;
    Ok(())
}

fn connect_via_http_connect(
    outbound: &OutboundConfig,
    host: &str,
    port: u16,
) -> io::Result<TcpStream> {
    let proxy = outbound.addr.as_ref().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "http_connect outbound missing addr",
        )
    })?;
    let mut stream = TcpStream::connect(proxy)?;
    stream.set_nodelay(true).ok();
    let authority = format!("{}:{}", host, port);
    let request = format!(
        "CONNECT {} HTTP/1.1\r\nHost: {}\r\nProxy-Connection: Keep-Alive\r\n\r\n",
        authority, authority
    );
    stream.write_all(request.as_bytes())?;

    let mut response = Vec::new();
    let mut buf = [0u8; 1];
    while response.len() < 8192 {
        stream.read_exact(&mut buf)?;
        response.push(buf[0]);
        if response.ends_with(b"\r\n\r\n") {
            break;
        }
    }
    let text = String::from_utf8_lossy(&response);
    if !text.starts_with("HTTP/1.1 200") && !text.starts_with("HTTP/1.0 200") {
        return Err(io::Error::new(
            io::ErrorKind::Other,
            format!(
                "HTTP CONNECT failed: {}",
                text.lines().next().unwrap_or("empty response")
            ),
        ));
    }
    Ok(stream)
}

fn choose_outbound(config: &Config, host: &str) -> String {
    let host_lc = host.trim_end_matches('.').to_ascii_lowercase();
    for rule in &config.rules {
        let value = rule.value.trim_start_matches('.').to_ascii_lowercase();
        let matched = match rule.rule_type {
            RuleType::DomainExact => host_lc == value,
            RuleType::DomainSuffix => host_lc == value || host_lc.ends_with(&format!(".{}", value)),
            RuleType::DomainContains => host_lc.contains(&value),
        };
        if matched {
            return rule.outbound.clone();
        }
    }
    config
        .outbounds
        .first()
        .map(|outbound| outbound.tag.clone())
        .unwrap_or_else(|| "direct".to_string())
}

fn append_stats(path: &str, stats: &ConnStats) -> io::Result<()> {
    ensure_stats_dir(path)?;
    let mut file = OpenOptions::new().create(true).append(true).open(path)?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_else(|_| Duration::from_secs(0))
        .as_millis();
    let started = now.saturating_sub(stats.elapsed_ms);
    writeln!(
        file,
        "{{\"started_at_unix_ms\":{},\"ended_at_unix_ms\":{},\"network\":\"{}\",\"target_host\":\"{}\",\"target_port\":{},\"domain\":\"{}\",\"port\":{},\"outbound\":\"{}\",\"uplink_bytes\":{},\"downlink_bytes\":{},\"elapsed_ms\":{},\"status\":\"{}\",\"error\":{}}}",
        started,
        now,
        json_escape(&stats.network),
        json_escape(&stats.domain),
        stats.port,
        json_escape(&stats.domain),
        stats.port,
        json_escape(&stats.outbound),
        stats.uplink_bytes,
        stats.downlink_bytes,
        stats.elapsed_ms,
        json_escape(&stats.status),
        match &stats.error {
            Some(err) => format!("\"{}\"", json_escape(err)),
            None => "null".to_string(),
        }
    )
}

fn json_escape(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c.is_control() => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

fn parse_config_path() -> PathBuf {
    let mut args = env::args().skip(1);
    while let Some(arg) = args.next() {
        if arg == "--config" || arg == "-c" {
            if let Some(path) = args.next() {
                return PathBuf::from(path);
            }
        }
    }
    PathBuf::from(DEFAULT_CONFIG_PATH)
}

fn load_runtime(path: &Path) -> io::Result<RuntimeState> {
    let config = parse_config(path)?;
    validate_config(&config)?;
    let outbounds = build_outbounds(&config)?;
    let modified = config_modified(path)?;
    Ok(RuntimeState {
        config,
        outbounds,
        modified,
        failed_modified: None,
    })
}

fn build_outbounds(config: &Config) -> io::Result<HashMap<String, OutboundConfig>> {
    let mut map = HashMap::new();
    for outbound in &config.outbounds {
        if map.insert(outbound.tag.clone(), outbound.clone()).is_some() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("duplicate outbound tag: {}", outbound.tag),
            ));
        }
    }
    Ok(map)
}

fn config_modified(path: &Path) -> io::Result<SystemTime> {
    fs::metadata(path)?.modified()
}

fn ensure_stats_dir(path: &str) -> io::Result<()> {
    if let Some(parent) = Path::new(path).parent() {
        fs::create_dir_all(parent)?;
    }
    Ok(())
}

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
enum Section {
    Root,
    Outbound,
    Rule,
}

fn parse_config(path: &Path) -> io::Result<Config> {
    let file = File::open(path)?;
    let reader = BufReader::new(file);
    let mut config = Config {
        listen: "127.0.0.1:10880".to_string(),
        stats_path: "/var/lib/v2ray-egress-sidecar/stats.jsonl".to_string(),
        reload_interval_secs: DEFAULT_RELOAD_INTERVAL_SECS,
        max_connections: 0,
        thread_stack_bytes: DEFAULT_THREAD_STACK_BYTES,
        tcp_idle_timeout_secs: DEFAULT_TCP_IDLE_TIMEOUT_SECS,
        tcp_half_close_timeout_secs: DEFAULT_TCP_HALF_CLOSE_TIMEOUT_SECS,
        udp_idle_timeout_secs: DEFAULT_UDP_IDLE_TIMEOUT_SECS,
        outbounds: Vec::new(),
        rules: Vec::new(),
    };
    let mut section = Section::Root;
    let mut outbound: Option<OutboundConfig> = None;
    let mut rule: Option<Rule> = None;

    for line in reader.lines() {
        let raw = line?;
        let line = strip_comment(&raw).trim().to_string();
        if line.is_empty() {
            continue;
        }
        if line.starts_with("[[") && line.ends_with("]]") {
            flush_section(&mut config, &mut outbound, &mut rule)?;
            let name = line.trim_start_matches("[[").trim_end_matches("]]").trim();
            section = match name {
                "outbounds" => {
                    outbound = Some(OutboundConfig {
                        tag: String::new(),
                        kind: OutboundKind::Direct,
                        addr: None,
                        username: None,
                        password: None,
                    });
                    Section::Outbound
                }
                "rules" | "routing.rules" => {
                    rule = Some(Rule {
                        rule_type: RuleType::DomainSuffix,
                        value: String::new(),
                        outbound: String::new(),
                    });
                    Section::Rule
                }
                _ => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        format!("unsupported section: {}", name),
                    ))
                }
            };
            continue;
        }

        let (key, value) = parse_key_value(&line)?;
        match section {
            Section::Root => apply_root(&mut config, &key, &value)?,
            Section::Outbound => {
                let current = outbound.as_mut().ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidInput, "outbound key outside section")
                })?;
                apply_outbound(current, &key, &value)?;
            }
            Section::Rule => {
                let current = rule.as_mut().ok_or_else(|| {
                    io::Error::new(io::ErrorKind::InvalidInput, "rule key outside section")
                })?;
                apply_rule(current, &key, &value)?;
            }
        }
    }
    flush_section(&mut config, &mut outbound, &mut rule)?;
    Ok(config)
}

fn flush_section(
    config: &mut Config,
    outbound: &mut Option<OutboundConfig>,
    rule: &mut Option<Rule>,
) -> io::Result<()> {
    if let Some(outbound) = outbound.take() {
        config.outbounds.push(outbound);
    }
    if let Some(rule) = rule.take() {
        config.rules.push(rule);
    }
    Ok(())
}

fn strip_comment(line: &str) -> String {
    let mut in_string = false;
    let mut escaped = false;
    let mut out = String::new();
    for ch in line.chars() {
        if escaped {
            out.push(ch);
            escaped = false;
            continue;
        }
        if ch == '\\' && in_string {
            out.push(ch);
            escaped = true;
            continue;
        }
        if ch == '"' {
            in_string = !in_string;
            out.push(ch);
            continue;
        }
        if ch == '#' && !in_string {
            break;
        }
        out.push(ch);
    }
    out
}

fn parse_key_value(line: &str) -> io::Result<(String, String)> {
    let mut parts = line.splitn(2, '=');
    let key = parts
        .next()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "empty key"))?;
    let value = parts
        .next()
        .map(|v| parse_value(v.trim()))
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "missing value"))?;
    Ok((key, value))
}

fn parse_value(value: &str) -> String {
    if value.starts_with('"') && value.ends_with('"') && value.len() >= 2 {
        let inner = &value[1..value.len() - 1];
        let mut out = String::new();
        let mut chars = inner.chars();
        while let Some(ch) = chars.next() {
            if ch == '\\' {
                match chars.next() {
                    Some('"') => out.push('"'),
                    Some('\\') => out.push('\\'),
                    Some('n') => out.push('\n'),
                    Some('r') => out.push('\r'),
                    Some('t') => out.push('\t'),
                    Some(other) => out.push(other),
                    None => out.push('\\'),
                }
            } else {
                out.push(ch);
            }
        }
        out
    } else {
        value.trim().to_string()
    }
}

fn apply_root(config: &mut Config, key: &str, value: &str) -> io::Result<()> {
    match key {
        "listen" => config.listen = value.to_string(),
        "stats_path" => config.stats_path = value.to_string(),
        "reload_interval_secs" => config.reload_interval_secs = parse_u64(key, value)?,
        "max_connections" => config.max_connections = parse_usize(key, value)?,
        "thread_stack_bytes" => config.thread_stack_bytes = parse_usize(key, value)?,
        "tcp_idle_timeout_secs" => config.tcp_idle_timeout_secs = parse_u64(key, value)?,
        "tcp_half_close_timeout_secs" => {
            config.tcp_half_close_timeout_secs = parse_u64(key, value)?
        }
        "udp_idle_timeout_secs" => config.udp_idle_timeout_secs = parse_u64(key, value)?,
        _ => {}
    }
    Ok(())
}

fn apply_outbound(outbound: &mut OutboundConfig, key: &str, value: &str) -> io::Result<()> {
    match key {
        "tag" => outbound.tag = value.to_string(),
        "kind" => {
            outbound.kind = match value {
                "direct" => OutboundKind::Direct,
                "socks5" => OutboundKind::Socks5,
                "http_connect" => OutboundKind::HttpConnect,
                _ => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        format!("unsupported outbound kind: {}", value),
                    ))
                }
            }
        }
        "addr" => outbound.addr = Some(value.to_string()),
        "username" => outbound.username = Some(value.to_string()),
        "password" => outbound.password = Some(value.to_string()),
        _ => {}
    }
    Ok(())
}

fn apply_rule(rule: &mut Rule, key: &str, value: &str) -> io::Result<()> {
    match key {
        "type" => {
            rule.rule_type = match value {
                "domain_exact" => RuleType::DomainExact,
                "domain_suffix" => RuleType::DomainSuffix,
                "domain_contains" => RuleType::DomainContains,
                _ => {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidInput,
                        format!("unsupported rule type: {}", value),
                    ))
                }
            }
        }
        "value" | "domain" => rule.value = value.to_string(),
        "domain_exact" => {
            rule.rule_type = RuleType::DomainExact;
            rule.value = value.to_string();
        }
        "domain_suffix" => {
            rule.rule_type = RuleType::DomainSuffix;
            rule.value = value.to_string();
        }
        "domain_contains" => {
            rule.rule_type = RuleType::DomainContains;
            rule.value = value.to_string();
        }
        "outbound" | "outbound_tag" => rule.outbound = value.to_string(),
        _ => {}
    }
    Ok(())
}

fn parse_u64(key: &str, value: &str) -> io::Result<u64> {
    value.parse::<u64>().map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("{} must be an integer: {}", key, value),
        )
    })
}

fn parse_usize(key: &str, value: &str) -> io::Result<usize> {
    value.parse::<usize>().map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("{} must be an integer: {}", key, value),
        )
    })
}

fn validate_config(config: &Config) -> io::Result<()> {
    config.listen.parse::<SocketAddr>().map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("listen must be host:port socket address: {}", config.listen),
        )
    })?;
    if config.outbounds.is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "at least one outbound is required",
        ));
    }
    let mut tags = HashMap::new();
    for outbound in &config.outbounds {
        if outbound.tag.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "outbound tag is required",
            ));
        }
        if tags.insert(outbound.tag.clone(), true).is_some() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("duplicate outbound tag: {}", outbound.tag),
            ));
        }
        if outbound.kind != OutboundKind::Direct && outbound.addr.is_none() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("outbound {} requires addr", outbound.tag),
            ));
        }
    }
    for rule in &config.rules {
        if rule.value.is_empty() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "rule value is required",
            ));
        }
        if !tags.contains_key(&rule.outbound) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("rule references missing outbound: {}", rule.outbound),
            ));
        }
    }
    Ok(())
}

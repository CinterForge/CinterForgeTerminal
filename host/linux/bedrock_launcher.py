"""Terminal LinuxKit native LAN relay and host launch payloads.
The launcher bytes are preserved from the working Mac guest integration.
Minecraft server acquisition and hosting policy belong to CraftMaster.
"""

LAN_RELAY_C = r'''#include <arpa/inet.h>
#include <netinet/in.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>

static volatile sig_atomic_t running = 1;
static char guest_ip[INET_ADDRSTRLEN];
static char host_ip[INET_ADDRSTRLEN];
static void on_signal(int sig) { (void)sig; running = 0; }
static int connect_guest(int port) {
    int fd=socket(AF_INET,SOCK_STREAM,0); if(fd<0)return -1;
    struct sockaddr_in a={0}; a.sin_family=AF_INET; a.sin_port=htons((uint16_t)port);
    if(inet_pton(AF_INET,guest_ip,&a.sin_addr)!=1 || connect(fd,(struct sockaddr*)&a,sizeof a)!=0){close(fd);return -1;} return fd;
}
struct tcp_pair{int client;int guest;};
static void *tcp_pair_main(void *opaque){
    struct tcp_pair*p=opaque; char buf[65536];
    while(running){fd_set r;FD_ZERO(&r);FD_SET(p->client,&r);FD_SET(p->guest,&r);int hi=p->client>p->guest?p->client:p->guest;if(select(hi+1,&r,NULL,NULL,NULL)<=0)break;int src=FD_ISSET(p->client,&r)?p->client:p->guest;int dst=src==p->client?p->guest:p->client;ssize_t n=recv(src,buf,sizeof buf,0);if(n<=0)break;char*q=buf;ssize_t left=n;while(left>0){ssize_t w=send(dst,q,(size_t)left,0);if(w<=0){left=-1;break;}q+=w;left-=w;}if(left<0)break;}
    close(p->client);close(p->guest);free(p);return NULL;
}
static void *tcp_main(void *opaque){
    int port=*(int*)opaque;int fd=socket(AF_INET,SOCK_STREAM,0),one=1;if(fd<0){running=0;return NULL;}setsockopt(fd,SOL_SOCKET,SO_REUSEADDR,&one,sizeof one);struct sockaddr_in a={0};a.sin_family=AF_INET;a.sin_port=htons((uint16_t)port);a.sin_addr.s_addr=htonl(INADDR_ANY);if(bind(fd,(struct sockaddr*)&a,sizeof a)!=0||listen(fd,32)!=0){perror("CINTER_BDS_LAN_RELAY tcp bind/listen");close(fd);running=0;return NULL;}fprintf(stderr,"CINTER_BDS_LAN_RELAY_TCP_READY port=%d guest=%s\n",port,guest_ip);fflush(stderr);
    while(running){struct timeval tv={1,0};fd_set r;FD_ZERO(&r);FD_SET(fd,&r);if(select(fd+1,&r,NULL,NULL,&tv)<=0)continue;struct sockaddr_in peer={0};socklen_t peer_len=sizeof peer;int client=accept(fd,(struct sockaddr*)&peer,&peer_len);if(client<0)continue;char peer_ip[INET_ADDRSTRLEN]={0};inet_ntop(AF_INET,&peer.sin_addr,peer_ip,sizeof peer_ip);fprintf(stderr,"CINTER_BDS_LAN_RELAY_TCP_CLIENT client=%s:%u port=%d\n",peer_ip,(unsigned)ntohs(peer.sin_port),port);fflush(stderr);int guest=connect_guest(port);if(guest<0){fprintf(stderr,"CINTER_BDS_LAN_RELAY_TCP_GUEST_UNREACHABLE port=%d guest=%s\n",port,guest_ip);fflush(stderr);close(client);continue;}struct tcp_pair*pair=malloc(sizeof*pair);if(!pair){close(client);close(guest);continue;}pair->client=client;pair->guest=guest;pthread_t t;if(pthread_create(&t,NULL,tcp_pair_main,pair)==0)pthread_detach(t);else{close(client);close(guest);free(pair);}}close(fd);return NULL;
}
struct udp_arg{int port;};
static void *udp_main(void *opaque){
    int port=((struct udp_arg*)opaque)->port;int fd=socket(AF_INET,SOCK_DGRAM,0),one=1;if(fd<0){running=0;return NULL;}setsockopt(fd,SOL_SOCKET,SO_REUSEADDR,&one,sizeof one);setsockopt(fd,SOL_SOCKET,SO_BROADCAST,&one,sizeof one);struct sockaddr_in bind_addr={0};bind_addr.sin_family=AF_INET;bind_addr.sin_port=htons((uint16_t)port);bind_addr.sin_addr.s_addr=htonl(INADDR_ANY);if(bind(fd,(struct sockaddr*)&bind_addr,sizeof bind_addr)!=0){perror("CINTER_BDS_LAN_RELAY udp bind");close(fd);running=0;return NULL;}struct sockaddr_in guest={0};guest.sin_family=AF_INET;guest.sin_port=htons((uint16_t)port);inet_pton(AF_INET,guest_ip,&guest.sin_addr);struct sockaddr_in client={0};socklen_t client_len=sizeof client;int have_client=0;fprintf(stderr,"CINTER_BDS_LAN_RELAY_UDP_READY port=%d guest=%s\n",port,guest_ip);fflush(stderr);char buf[65536];
    while(running){struct timeval tv={1,0};fd_set r;FD_ZERO(&r);FD_SET(fd,&r);if(select(fd+1,&r,NULL,NULL,&tv)<=0)continue;struct sockaddr_in src={0};socklen_t slen=sizeof src;ssize_t n=recvfrom(fd,buf,sizeof buf,0,(struct sockaddr*)&src,&slen);if(n<=0)continue;if(src.sin_addr.s_addr==guest.sin_addr.s_addr){if(have_client)sendto(fd,buf,(size_t)n,0,(struct sockaddr*)&client,client_len);}else{if(!have_client){char addr[INET_ADDRSTRLEN]={0};inet_ntop(AF_INET,&src.sin_addr,addr,sizeof addr);fprintf(stderr,"CINTER_BDS_LAN_RELAY_UDP_CLIENT client=%s:%u port=%d\n",addr,(unsigned)ntohs(src.sin_port),port);fflush(stderr);}client=src;client_len=slen;have_client=1;sendto(fd,buf,(size_t)n,0,(struct sockaddr*)&guest,sizeof guest);}}close(fd);return NULL;
}
static void *discovery_main(void *opaque){
    (void)opaque;int port=7551;int fd=socket(AF_INET,SOCK_DGRAM,0),one=1;if(fd<0){running=0;return NULL;}setsockopt(fd,SOL_SOCKET,SO_REUSEADDR,&one,sizeof one);setsockopt(fd,SOL_SOCKET,SO_BROADCAST,&one,sizeof one);struct sockaddr_in bind_addr={0};bind_addr.sin_family=AF_INET;bind_addr.sin_port=htons((uint16_t)port);bind_addr.sin_addr.s_addr=htonl(INADDR_ANY);if(bind(fd,(struct sockaddr*)&bind_addr,sizeof bind_addr)!=0){perror("CINTER_BDS_LAN_DISCOVERY bind");close(fd);running=0;return NULL;}struct sockaddr_in guest={0};guest.sin_family=AF_INET;guest.sin_port=htons((uint16_t)port);inet_pton(AF_INET,guest_ip,&guest.sin_addr);struct sockaddr_in broadcast={0};broadcast.sin_family=AF_INET;broadcast.sin_port=htons((uint16_t)port);broadcast.sin_addr.s_addr=htonl(INADDR_BROADCAST);fprintf(stderr,"CINTER_BDS_LAN_DISCOVERY_READY port=7551 guest=%s\n",guest_ip);fflush(stderr);char buf[65536];int reported_client=0;
    while(running){struct timeval tv={1,0};fd_set r;FD_ZERO(&r);FD_SET(fd,&r);if(select(fd+1,&r,NULL,NULL,&tv)<=0)continue;struct sockaddr_in src={0};socklen_t slen=sizeof src;ssize_t n=recvfrom(fd,buf,sizeof buf,0,(struct sockaddr*)&src,&slen);if(n<=0)continue;char src_ip[INET_ADDRSTRLEN]={0};inet_ntop(AF_INET,&src.sin_addr,src_ip,sizeof src_ip);if(src.sin_addr.s_addr==guest.sin_addr.s_addr){if(sendto(fd,buf,(size_t)n,0,(struct sockaddr*)&broadcast,sizeof broadcast)<0)perror("CINTER_BDS_LAN_DISCOVERY broadcast sendto");}else if(strcmp(src_ip,host_ip)==0){/* Loopback broadcast; do not flood the interactive console. */}else{if(!reported_client){fprintf(stderr,"CINTER_BDS_LAN_DISCOVERY_CLIENT client=%s:%u\n",src_ip,(unsigned)ntohs(src.sin_port));fflush(stderr);reported_client=1;}if(sendto(fd,buf,(size_t)n,0,(struct sockaddr*)&guest,sizeof guest)<0)perror("CINTER_BDS_LAN_DISCOVERY guest sendto");}}
    close(fd);return NULL;
}
static int read_guest_ip(const char*path){for(int i=0;i<300&&running;++i){FILE*f=fopen(path,"r");if(f){char line[128]={0};if(fgets(line,sizeof line,f)){line[strcspn(line,"\r\n")]=0;struct in_addr a;if(inet_pton(AF_INET,line,&a)==1){snprintf(guest_ip,sizeof guest_ip,"%s",line);fclose(f);return 0;}}fclose(f);}usleep(100000);}return -1;}
/* Port configuration is supplied by the existing host launcher, reading the
 * same native server.properties BDS will load. No property rewrites. */
static int parse_port(const char *value) {
    char *end = NULL; long n = strtol(value, &end, 10);
    return end && *value && !*end && n > 0 && n <= 65535 ? (int)n : 0;
}
static void add_port(int *ports, size_t *count, int port) {
    if (!port) return;
    for (size_t i=0; i<*count; ++i) if (ports[i]==port) return;
    ports[(*count)++] = port;
}
int main(int argc, char **argv) {
    if (argc != 7) {
        fprintf(stderr, "usage: %s guest-ip-file host-ip server-port server-portv6 lan-visible debug-port-or-zero\n", argv[0]);
        return 2;
    }
    int tcp_ports[2], ports[24]; size_t ntcp=0, nports=0;
    int port4=parse_port(argv[3]), port6=parse_port(argv[4]);
    int lan=!strcmp(argv[5], "true"), debug=parse_port(argv[6]);
    if (!port4 || !port6 || (!lan && strcmp(argv[5], "false")) || (!debug && strcmp(argv[6], "0"))) {
        fprintf(stderr, "CINTER_BDS_LAN_RELAY_ERROR invalid port or visibility setting\n"); return 2;
    }
    add_port(tcp_ports, &ntcp, port4); add_port(tcp_ports, &ntcp, debug);
    add_port(ports, &nports, port4); add_port(ports, &nports, port6);
    if (lan) { add_port(ports, &nports, 19132); add_port(ports, &nports, 19133); }
    for (int p=19140; p<=19155; ++p) add_port(ports, &nports, p);
    struct in_addr host_addr;
    if (inet_pton(AF_INET, argv[2], &host_addr)!=1) {
        fprintf(stderr, "CINTER_BDS_LAN_RELAY_ERROR invalid-host-ip %s\n", argv[2]); return 6;
    }
    snprintf(host_ip, sizeof host_ip, "%s", argv[2]);
    signal(SIGINT, on_signal); signal(SIGTERM, on_signal);
    if (read_guest_ip(argv[1])!=0) {
        fprintf(stderr, "CINTER_BDS_LAN_RELAY_ERROR guest-ip-timeout path=%s\n", argv[1]); return 3;
    }
    fprintf(stderr, "CINTER_BDS_LAN_RELAY_GUEST guest=%s host=%s\n", guest_ip, host_ip); fflush(stderr);
    pthread_t tcp[2], discovery, threads[24]; struct udp_arg args[24];
    size_t tcp_started=0, udp_started=0; int discovery_started=0, failure=0;
    for (size_t i=0; i<ntcp; ++i) {
        if (pthread_create(&tcp[i], NULL, tcp_main, &tcp_ports[i])!=0) { failure=1; running=0; break; }
        ++tcp_started;
    }
    /* If the user assigns 7551 as a gameplay port, do not also bind it as
     * the auxiliary LAN broadcast relay. The BDS gameplay port wins. */
    if (running && lan && port4!=7551 && port6!=7551) {
        if (pthread_create(&discovery, NULL, discovery_main, NULL)!=0) { failure=1; running=0; }
        else discovery_started=1;
    }
    for (size_t i=0; running && i<nports; ++i) {
        args[i].port=ports[i];
        if (pthread_create(&threads[i], NULL, udp_main, &args[i])!=0) { failure=1; running=0; break; }
        ++udp_started;
    }
    while (running) sleep(1);
    for (size_t i=0; i<tcp_started; ++i) pthread_join(tcp[i], NULL);
    if (discovery_started) pthread_join(discovery, NULL);
    for (size_t i=0; i<udp_started; ++i) pthread_join(threads[i], NULL);
    return failure ? 4 : 0;
}
'''

RUN_SH = r'''#!/bin/sh
set -eu
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
BDS=${1:?usage: run-mac-bds.sh /path/to/extracted/Linux/Server}
BDS=$(cd "$BDS" 2>/dev/null && pwd -P) || { echo "CINTER_VM_ERROR BDS server directory not found: $1" >&2; exit 59; }
echo "CINTER_BDS_HOST_ROOT $BDS"
[ -x "$HERE/CinterBDSVM" ] || { echo "CINTER_VM_ERROR missing $HERE/CinterBDSVM" >&2; exit 50; }
[ -s "$HERE/cinter-bds-kernel" ] || { echo "CINTER_VM_ERROR missing $HERE/cinter-bds-kernel" >&2; exit 51; }
[ -s "$HERE/cinter-bds-initrd.img" ] || { echo "CINTER_VM_ERROR missing $HERE/cinter-bds-initrd.img" >&2; exit 52; }
[ -s "$HERE/cinter-bds-cmdline" ] || { echo "CINTER_VM_ERROR missing $HERE/cinter-bds-cmdline" >&2; exit 54; }
[ -x "$BDS/bedrock_server" ] || { echo "CINTER_VM_ERROR missing $BDS/bedrock_server" >&2; exit 53; }
mkdir -p "$BDS/.cinter"
[ -d "$HERE/glibc-x86_64" ] || { echo "CINTER_VM_ERROR missing $HERE/glibc-x86_64" >&2; exit 55; }
# Only immutable runtime libraries are replaced; worlds and user data are untouched.
rm -rf "$BDS/.cinter/glibc-x86_64"
cp -R "$HERE/glibc-x86_64" "$BDS/.cinter/glibc-x86_64"
cp "$HERE/cinter-nethernet-probe-linux-arm64" "$BDS/.cinter/cinter-nethernet-probe"
chmod 755 "$BDS/.cinter/cinter-nethernet-probe"
rm -f "$BDS/.cinter/guest-ip" "$BDS/.cinter/vm-stop-request"
# Locate the host LAN IPv4 address without touching the player's properties.
DEFAULT_IF=$(/sbin/route -n get default 2>/dev/null | awk '/interface:/{print $2; exit}')
[ -n "$DEFAULT_IF" ] || { echo "CINTER_VM_ERROR cannot determine macOS default LAN interface" >&2; exit 57; }
HOST_IP=$(/usr/sbin/ipconfig getifaddr "$DEFAULT_IF" 2>/dev/null || true)
printf '%s\n' "$HOST_IP" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' || { echo "CINTER_VM_ERROR cannot determine LAN IPv4 on $DEFAULT_IF" >&2; exit 58; }
[ -x "$HERE/CinterBDSNetRelay" ] || { echo "CINTER_VM_ERROR missing Setup-built LAN relay" >&2; exit 56; }
# CINTER_BDS_PROPERTY_PORTS_V1: native property values, not relay constants.
read_property() {
  awk -F= -v key="$1" -v fallback="$2" '
    { name=$1; gsub(/^[ \t]+|[ \t]+$/, "", name) }
    name==key { value=substr($0,index($0,"=")+1); gsub(/^[ \t]+|[ \t\r]+$/, "",value); found=1 }
    END { print found ? value : fallback }' "$BDS/server.properties"
}
PORT4=$(read_property server-port 19132)
PORT6=$(read_property server-portv6 19133)
LAN_VISIBLE=$(read_property enable-lan-visibility true)
DEBUG_PORT=0
if [ "$(read_property allow-inbound-script-debugging false)" = true ]; then
  DEBUG_PORT=$(read_property force-inbound-debug-port 19144)
fi
"$HERE/CinterBDSNetRelay" "$BDS/.cinter/guest-ip" "$HOST_IP" "$PORT4" "$PORT6" "$LAN_VISIBLE" "$DEBUG_PORT" &
RELAY_PID=$!
cleanup() { rc=$?; trap - EXIT INT TERM; kill "$RELAY_PID" 2>/dev/null || true; wait "$RELAY_PID" 2>/dev/null || true; exit "$rc"; }
trap cleanup EXIT INT TERM
export CINTER_VM_KERNEL="$HERE/cinter-bds-kernel"
export CINTER_VM_INITRD="$HERE/cinter-bds-initrd.img"
export CINTER_VM_CMDLINE="$HERE/cinter-bds-cmdline"
export CINTER_BDS_HOST_DIR="$BDS"
# No restricted Apple networking entitlement is necessary. The same host
# relay used by the original Mac pipeline exposes Bedrock TCP, gameplay UDP,
# and discovery UDP; the guest itself has a private VZ address.
echo "CINTER_BDS_NETWORK host-relay host=$HOST_IP tcp=$PORT4 udp=$PORT4,$PORT6,19140-19155 lan-visible=$LAN_VISIBLE debug-port=$DEBUG_PORT"
# This process stays attached to the VM serial console. If it exits early,
# BedrockManager immediately reports the error rather than claiming a server.
"$HERE/CinterBDSVM"
'''

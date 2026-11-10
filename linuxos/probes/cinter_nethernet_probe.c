#include <arpa/inet.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

static int probe(int family, int port) {
    int s = socket(family, SOCK_STREAM, 0);
    if (s < 0) return 2;
    struct timeval tv = {0, 500000};
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    int rc;
    if (family == AF_INET6) {
        struct sockaddr_in6 a = {0};
        a.sin6_family = AF_INET6; a.sin6_port = htons((uint16_t)port); a.sin6_addr = in6addr_loopback;
        rc = connect(s, (struct sockaddr *)&a, sizeof a);
    } else {
        struct sockaddr_in a = {0};
        a.sin_family = AF_INET; a.sin_port = htons((uint16_t)port); a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        rc = connect(s, (struct sockaddr *)&a, sizeof a);
    }
    int saved = errno;
    close(s);
    if (rc == 0) return 0;
    fprintf(stderr, "nethernet signaling listener missing family=%s errno=%d (%s)\n",
            family == AF_INET6 ? "ipv6" : "ipv4", saved, strerror(saved));
    return 3;
}

int main(int argc, char **argv) {
    int port = argc > 1 ? atoi(argv[1]) : 19132;
    if (port < 1 || port > 65535) return 6;
    int rc6 = probe(AF_INET6, port);
    int rc4 = probe(AF_INET, port);
    if (rc6 == 0 || rc4 == 0) {
        puts("CINTER_BDS_NETHERNET_PASS");
        return 0;
    }
    fprintf(stderr, "CINTER_BDS_NETHERNET_PROBE_FAIL ipv6=%d ipv4=%d port=%d\n", rc6, rc4, port);
    return 4;
}

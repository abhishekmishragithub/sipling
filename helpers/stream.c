/* MIT source; linking this helper with pjproject creates a GPL/commercial
 * executable. Build separately, never bundle it in sipling distributions. */
#include <pjsua-lib/pjsua.h>
#include <sys/socket.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>

static void timestamp(unsigned char *out) {
    struct timespec ts;
    uint64_t ns;
    unsigned i;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) _exit(6);
    ns = (uint64_t)ts.tv_sec * 1000000000 + (uint64_t)ts.tv_nsec;
    for (i = 0; i < 8; ++i) out[7-i] = (unsigned char)(ns >> (8*i));
}
static void log_writer(int level, const char *data, int len) {
    PJ_UNUSED_ARG(level);
    fwrite(data, 1, (size_t)len, stdout); fflush(stdout);
}
static int media_fd;
static pjsua_call_id current = PJSUA_INVALID_ID;
static pjsua_conf_port_id slot;
static pjmedia_port port;
static pj_pool_t *pool;
static void event(const char *name, int code) {
    printf("%s %d\n", name, code); fflush(stdout);
}
static pj_status_t get_frame(pjmedia_port *p, pjmedia_frame *f) {
    unsigned char packet[325];
    unsigned char ack[13];
    ssize_t n;
    PJ_UNUSED_ARG(p);
    memset(f->buf, 0, 320);
    n = recv(media_fd, packet, sizeof(packet), MSG_DONTWAIT);
    if (n == 325 && packet[0] == 'S') {
        /* IPC is explicitly little endian; pjmedia uses native signed PCM. */
        short *samples = (short *)f->buf;
        unsigned i;
        for (i = 0; i < 160; ++i)
            samples[i] = (short)(packet[5+2*i] | packet[6+2*i] << 8);
        memcpy(ack, packet, 5); ack[0] = 'A';
        timestamp(ack + 5);
        if (send(media_fd, ack, sizeof(ack), MSG_DONTWAIT) != sizeof(ack)) _exit(3);
    } else if (n > 0) {
        _exit(4);
    }
    f->type = PJMEDIA_FRAME_TYPE_AUDIO;
    f->size = 320;
    return PJ_SUCCESS;
}
static pj_status_t put_frame(pjmedia_port *p, pjmedia_frame *f) {
    unsigned char packet[329] = {'P'};
    unsigned i;
    PJ_UNUSED_ARG(p);
    timestamp(packet + 1);
    if (f->type == PJMEDIA_FRAME_TYPE_AUDIO && f->size == 320) {
        const short *samples = (const short *)f->buf;
        for (i = 0; i < 160; ++i) {
            packet[9+2*i] = (unsigned char)samples[i];
            packet[10+2*i] = (unsigned char)((unsigned short)samples[i] >> 8);
        }
    }
    /* Never block the media clock or silently drop a frame on IPC overflow. */
    if (send(media_fd, packet, sizeof(packet), MSG_DONTWAIT) != sizeof(packet)) _exit(3);
    return PJ_SUCCESS;
}
static void state(pjsua_call_id id, pjsip_event *e) {
    pjsua_call_info info;
    PJ_UNUSED_ARG(e);
    if (pjsua_call_get_info(id, &info) != PJ_SUCCESS) return;
    if (info.state == PJSIP_INV_STATE_EARLY) event("RINGING", info.last_status);
    if (info.state == PJSIP_INV_STATE_CONFIRMED) event("ANSWERED", info.last_status);
    if (info.state == PJSIP_INV_STATE_DISCONNECTED) event("ENDED", info.last_status);
}
static void transaction(pjsua_call_id id, pjsip_transaction *tsx, pjsip_event *e) {
    PJ_UNUSED_ARG(id); PJ_UNUSED_ARG(e);
    if (tsx->method.id == PJSIP_BYE_METHOD && tsx->status_code >= 200 &&
        tsx->status_code < 300 && tsx->state >= PJSIP_TSX_STATE_COMPLETED)
        event("CLEAN", tsx->status_code);
}
static void media(pjsua_call_id id) {
    pjsua_call_info info;
    if (pjsua_call_get_info(id, &info) != PJ_SUCCESS) return;
    if (info.media_status == PJSUA_CALL_MEDIA_ACTIVE) {
        if (pjsua_conf_connect(info.conf_slot, slot) != PJ_SUCCESS ||
            pjsua_conf_connect(slot, info.conf_slot) != PJ_SUCCESS) _exit(5);
    }
}
static int check(pj_status_t status) { return status == PJ_SUCCESS; }
int main(int argc, char **argv) {
    pjsua_config cfg;
    pjsua_logging_config log;
    pjsua_media_config mc;
    pjsua_transport_config tc;
    pjsua_transport_id tid;
    pjsua_acc_config ac;
    pjsua_acc_id aid;
    pj_str_t name = pj_str("sipling-pcm");
    pj_str_t codec = pj_str("*");
    pj_str_t target;
    char line[128];
    if (argc != 4) return 2;
    media_fd = atoi(argv[1]);
    if (!check(pjsua_create())) return 2;
    pjsua_config_default(&cfg);
    cfg.max_calls = 1;
    cfg.cb.on_call_state = state;
    cfg.cb.on_call_tsx_state = transaction;
    cfg.cb.on_call_media_state = media;
    pjsua_logging_config_default(&log);
    log.console_level = 5; log.level = 5; log.cb = log_writer;
    pjsua_media_config_default(&mc);
    mc.clock_rate = mc.snd_clock_rate = 8000;
    mc.channel_count = 1; mc.audio_frame_ptime = 20;
    mc.no_vad = PJ_TRUE; mc.ec_tail_len = 0;
    if (!check(pjsua_init(&cfg, &log, &mc))) return 2;
    pjsua_transport_config_default(&tc);
    tc.port = 0;
    if (!check(pjsua_transport_create(strcmp(argv[2], "tcp") == 0 ?
              PJSIP_TRANSPORT_TCP : PJSIP_TRANSPORT_UDP, &tc, &tid))) return 2;
    pjsua_acc_config_default(&ac);
    ac.id = pj_str("sip:sipling@localhost"); ac.transport_id = tid;
    ac.allow_contact_rewrite = PJ_FALSE;
    if (!check(pjsua_acc_add(&ac, PJ_TRUE, &aid))) return 2;
    if (!check(pjsua_start()) || !check(pjsua_set_null_snd_dev())) return 2;
    pjsua_codec_set_priority(&codec, 0);
    codec = pj_str("PCMU/8000");
    if (!check(pjsua_codec_set_priority(&codec, 250))) return 2;
    pool = pjsua_pool_create("pcm", 2048, 2048);
    if (!pool) return 2;
    pjmedia_port_info_init(&port.info, &name, PJMEDIA_SIG_CLASS_PORT_AUD('S','P'),
                           8000, 1, 16, 160);
    port.get_frame = get_frame; port.put_frame = put_frame;
    if (!check(pjsua_conf_add_port(pool, &port, &slot))) return 2;
    target = pj_str(argv[3]);
    event("DIALED", 0);
    if (!check(pjsua_call_make_call(aid, &target, 0, NULL, NULL, &current))) return 2;
    while (fgets(line, sizeof(line), stdin)) {
        if (line[0] == 'q') break;
        if (line[0] == 'h') pjsua_call_hangup(current, 0, NULL, NULL);
        if (line[0] == 'd') {
            pj_str_t digits;
            line[strcspn(line, "\r\n")] = 0;
            digits = pj_str(line + 1);
            event("DTMF_RESULT", pjsua_call_dial_dtmf(current, &digits));
        }
    }
    pjsua_conf_remove_port(slot);
    pj_pool_release(pool);
    pjsua_destroy();
    return 0;
}

// duxbot_bridge.cpp
// Booster Audio SDK bridge: captures NAEC PCM → /tmp/duxbot_cap FIFO
//                           reads PCM from /tmp/duxbot_play FIFO → AudioPlayer

#include <booster/robot/channel/channel_factory.hpp>
#include <booster/robot/audio/audio_manager.h>
#include <booster/robot/audio/audio_capture_stream.h>
#include <booster/robot/audio/audio_player.h>
#include <booster/robot/audio/audio_types.h>

#include <atomic>
#include <csignal>
#include <fcntl.h>
#include <iostream>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>

using namespace booster::robot::audio;

static std::atomic<bool> g_running{true};

static void on_signal(int) { g_running = false; }

int main() {
    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);
    signal(SIGPIPE, SIG_IGN);  // ignore broken pipe when voice process restarts

    // Init Booster DDS channel (domain 0, on-robot)
    booster::robot::ChannelFactory::Instance()->Init(0);

    // Create FIFOs (ignore error if already exist)
    mkfifo("/tmp/duxbot_cap",  0666);
    mkfifo("/tmp/duxbot_play", 0666);

    std::cout << "[bridge] Waiting for Python client to open capture FIFO..." << std::endl;

    // Open capture FIFO for writing (blocks until Python opens for reading)
    int cap_fd = open("/tmp/duxbot_cap", O_WRONLY);
    if (cap_fd < 0) { perror("[bridge] open cap_fd"); return 1; }
    std::cout << "[bridge] Capture FIFO ready." << std::endl;

    // Init AudioManager
    AudioManager mgr;
    if (mgr.Init() != 0) {
        std::cerr << "[bridge] AudioManager::Init() failed" << std::endl;
        return 1;
    }

    // Capture stream — NAEC (echo-cancelled, post-BoosterAEC)
    auto capture = mgr.CreateCaptureStream();
    AudioCaptureStreamOptions cap_opts;
    cap_opts.enable_raw_pcm   = false;
    cap_opts.enable_naec_pcm  = true;
    cap_opts.requested_raw_format = {16000, 1, 16};

    capture->SetFrameCallback([&cap_fd](const AudioCaptureFrame &frame) {
        if (!g_running) return;
        if (!frame.naec_valid || frame.naec_pcm.empty()) return;
        const uint8_t *data = reinterpret_cast<const uint8_t *>(frame.naec_pcm.data());
        size_t size = frame.naec_pcm.size() * sizeof(int16_t);
        // Non-blocking write — if FIFO is full, drop the frame
        ssize_t written = write(cap_fd, data, size);
        (void)written;
    });

    capture->SetErrorCallback([](const AudioError &err) {
        std::cerr << "[bridge] Capture error: " << err.ret_msg << std::endl;
    });

    if (capture->Init(cap_opts) != 0) {
        std::cerr << "[bridge] CaptureStream::Init() failed" << std::endl;
        return 1;
    }
    capture->Start();
    std::cout << "[bridge] Capture started (16kHz NAEC)." << std::endl;

    // Player — 24kHz mono (xAI output format)
    auto player = mgr.CreatePlayer();
    PlayerInitOptions play_opts;
    play_opts.source_type    = AudioSourceType::kPcmStream;
    play_opts.sample_rate_hz = 24000;
    play_opts.channels       = 1;
    play_opts.bits_per_sample = 16;
    play_opts.priority       = PlayerPriority::kHigh;

    if (player->Init(play_opts) != 0) {
        std::cerr << "[bridge] AudioPlayer::Init() failed" << std::endl;
        return 1;
    }
    player->Start();

    // Playback thread — opens FIFO (blocks until Python opens write end)
    std::thread play_thread([&]() {
        std::cout << "[bridge] Waiting for Python client to open playback FIFO..." << std::endl;
        int play_fd = open("/tmp/duxbot_play", O_RDONLY);
        if (play_fd < 0) { perror("[bridge] open play_fd"); return; }
        std::cout << "[bridge] Playback FIFO ready." << std::endl;

        uint8_t buf[4096];
        ssize_t n;
        while (g_running) {
            n = read(play_fd, buf, sizeof(buf));
            if (n <= 0) break;
            player->PushPcmStream(buf, static_cast<size_t>(n));
        }
        close(play_fd);
    });

    std::cout << "[bridge] Running. Send SIGTERM to stop." << std::endl;
    while (g_running) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }

    std::cout << "[bridge] Shutting down..." << std::endl;
    capture->Stop();
    capture->Destroy();
    player->Stop();
    player->Destroy();
    mgr.Shutdown();
    play_thread.join();
    close(cap_fd);
    unlink("/tmp/duxbot_cap");
    unlink("/tmp/duxbot_play");

    return 0;
}

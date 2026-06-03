// Minimal v8-jsi inspector smoke test.
// Creates a runtime with inspector enabled on a configurable port, keeps the
// JS thread alive ticking a counter, and pumps any inspector-posted tasks
// onto the JS thread so CDP messages get serviced.
//
// Usage: ./smoke [port=9229] [seconds=30]
// Verify with: curl -s http://localhost:9229/json

#include <jsi/jsi.h>
#include "public/V8JsiRuntime.h"

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstdlib>
#include <iostream>
#include <memory>
#include <mutex>
#include <queue>
#include <thread>

class QueueTaskRunner : public v8runtime::JSITaskRunner {
 public:
  void postTask(std::unique_ptr<v8runtime::JSITask> task) override {
    std::lock_guard<std::mutex> g(m_);
    q_.push(std::move(task));
  }
  void drain() {
    std::queue<std::unique_ptr<v8runtime::JSITask>> local;
    {
      std::lock_guard<std::mutex> g(m_);
      local.swap(q_);
    }
    while (!local.empty()) {
      local.front()->run();
      local.pop();
    }
  }
 private:
  std::mutex m_;
  std::queue<std::unique_ptr<v8runtime::JSITask>> q_;
};

static std::atomic<bool> g_stop{false};
static void on_sig(int) { g_stop = true; }

int main(int argc, char** argv) {
  uint16_t port = (argc > 1) ? static_cast<uint16_t>(std::atoi(argv[1])) : 9229;
  int seconds = (argc > 2) ? std::atoi(argv[2]) : 30;
  std::signal(SIGINT, on_sig);
  std::signal(SIGTERM, on_sig);

  auto runner = std::make_shared<QueueTaskRunner>();
  v8runtime::V8RuntimeArgs args;
  args.foreground_task_runner = runner;
  args.inspectorPort = port;
  args.debuggerRuntimeName = "smoke";
  args.flags.enableInspector = true;
  args.flags.waitForDebugger = false;

  auto rt = v8runtime::makeV8Runtime(std::move(args));
  std::cout << "[smoke] inspector listening on port " << port
            << " for up to " << seconds << "s" << std::endl;
  std::cout.flush();

  rt->evaluateJavaScript(
      std::make_shared<facebook::jsi::StringBuffer>(std::string("globalThis.n = 0;")),
      "init.js");

  auto end = std::chrono::steady_clock::now() + std::chrono::seconds(seconds);
  while (!g_stop && std::chrono::steady_clock::now() < end) {
    runner->drain();
    rt->evaluateJavaScript(
        std::make_shared<facebook::jsi::StringBuffer>(std::string("globalThis.n++;")),
        "tick.js");
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }
  std::cout << "[smoke] done" << std::endl;
  return 0;
}

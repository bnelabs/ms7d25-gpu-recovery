#include <cuda_runtime.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#define CUDA_CHECK(call) do { const auto err = (call); if (err != cudaSuccess) { std::fprintf(stderr, "%s: %s\n", #call, cudaGetErrorString(err)); std::exit(1); } } while (0)

__global__ void verify_execution(unsigned *values) {
    unsigned i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < 1024) values[i] = i * 17u + 7u;
}

int main() {
    cudaDeviceProp props{};
    CUDA_CHECK(cudaGetDeviceProperties(&props, 0));
    std::printf("GPU: %s\nCompute capability: %d.%d\nVRAM: %.3f GiB\nSMs: %d\nUnified addressing: %d\nManaged memory: %d\n",
                props.name, props.major, props.minor, props.totalGlobalMem / double(1ULL << 30),
                props.multiProcessorCount, props.unifiedAddressing, props.managedMemory);
    constexpr size_t bytes = 256ULL << 20;
    constexpr int repeats = 32;
    void *host, *device, *second;
    CUDA_CHECK(cudaMallocHost(&host, bytes));
    CUDA_CHECK(cudaMalloc(&device, bytes));
    CUDA_CHECK(cudaMalloc(&second, bytes));
    std::memset(host, 42, bytes);
    cudaEvent_t start, stop;
    CUDA_CHECK(cudaEventCreate(&start));
    CUDA_CHECK(cudaEventCreate(&stop));
    const cudaMemcpyKind kinds[] = {cudaMemcpyHostToDevice, cudaMemcpyDeviceToHost, cudaMemcpyDeviceToDevice};
    const char *names[] = {"Pinned host -> GPU", "GPU -> pinned host", "GPU -> GPU"};
    for (int test = 0; test < 3; ++test) {
        void *dst = test == 0 ? device : test == 1 ? host : second;
        const void *src = test == 0 ? host : device;
        CUDA_CHECK(cudaMemcpy(dst, src, bytes, kinds[test]));
        CUDA_CHECK(cudaEventRecord(start));
        for (int iteration = 0; iteration < repeats; ++iteration) {
            CUDA_CHECK(cudaMemcpyAsync(dst, src, bytes, kinds[test]));
        }
        CUDA_CHECK(cudaEventRecord(stop));
        CUDA_CHECK(cudaEventSynchronize(stop));
        float ms;
        CUDA_CHECK(cudaEventElapsedTime(&ms, start, stop));
        std::printf("%s: %.3f GB/s (payload only)\n", names[test], bytes * double(repeats) / (ms * 1e6));
    }
    CUDA_CHECK(cudaMemcpy(host, second, bytes, cudaMemcpyDeviceToHost));
    for (size_t i = 0; i < bytes; ++i) {
        if (static_cast<unsigned char *>(host)[i] != 42) return 2;
    }
    verify_execution<<<4,256>>>(static_cast<unsigned *>(device));
    CUDA_CHECK(cudaGetLastError());
    CUDA_CHECK(cudaDeviceSynchronize());
    unsigned computed[1024];
    CUDA_CHECK(cudaMemcpy(computed, device, sizeof(computed), cudaMemcpyDeviceToHost));
    for (unsigned i = 0; i < 1024; ++i) {
        if (computed[i] != i * 17u + 7u) return 3;
    }
    std::puts("Kernel correctness check: passed");
    CUDA_CHECK(cudaEventDestroy(start));
    CUDA_CHECK(cudaEventDestroy(stop));
    CUDA_CHECK(cudaFree(device));
    CUDA_CHECK(cudaFree(second));
    CUDA_CHECK(cudaFreeHost(host));
    std::puts("Transfer correctness check: passed");
}

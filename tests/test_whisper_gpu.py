"""NVIDIA CUDA selection and diagnostics for Whisper caption generation."""
import types
import unittest

from spankvanc.engine import plan, validate
from spankvanc.whisper_worker import cuda_status
from test_caption_tcp import STOCK, tcp_route


def fake_torch(built, available=False, names=(), fail=False):
    def is_available():
        if fail:
            raise RuntimeError('driver/library version mismatch')
        return available
    cuda = types.SimpleNamespace(is_available=is_available, device_count=lambda: len(names),
                                 get_device_name=lambda i: names[i])
    return types.SimpleNamespace(version=types.SimpleNamespace(cuda=built), cuda=cuda)


GPU = dict(available=True, cuda=True, torch_cuda='12.4', gpus=['NVIDIA RTX A4000'], models=['small.en', 'turbo'])
CPU_WHEEL = dict(available=True, cuda=False, torch_cuda=None, gpus=[], models=['small.en', 'turbo'],
                 cuda_error='PyTorch in the Whisper Python is a CPU-only build; install the CUDA build of torch')


class CudaStatusTests(unittest.TestCase):
    def test_distinguishes_cpu_wheel_missing_gpu_and_working_gpu(self):
        self.assertIn('CPU-only build', cuda_status(fake_torch(None))['cuda_error'])
        self.assertIn('nvidia-smi', cuda_status(fake_torch('12.4'))['cuda_error'])
        self.assertIn('version mismatch', cuda_status(fake_torch('12.4', fail=True))['cuda_error'])
        ok = cuda_status(fake_torch('12.4', True, ['NVIDIA RTX A4000', 'Tesla T4']))
        self.assertEqual((ok['cuda'], ok['gpus'], ok.get('cuda_error')), (True, ['NVIDIA RTX A4000', 'Tesla T4'], None))


class PlanTests(unittest.TestCase):
    def route(self, device, model='small.en'):
        cfg = tcp_route()
        cfg['whisper'].update(device=device, model=model)
        return validate(cfg)

    def test_auto_reports_gpu(self):
        result = plan(self.route('auto', 'turbo'), dict(STOCK, whisper=GPU))
        self.assertTrue(result['ready'], result['blockers'])
        self.assertTrue(any('runs on the GPU: NVIDIA RTX A4000' in n for n in result['notes']))
        self.assertFalse(any('unlikely to keep up' in n for n in result['notes']))

    def test_auto_explains_cpu_fallback_and_large_model(self):
        result = plan(self.route('auto', 'turbo'), dict(STOCK, whisper=CPU_WHEEL))
        self.assertTrue(result['ready'])
        self.assertTrue(any('runs on the CPU: PyTorch' in n for n in result['notes']))
        self.assertTrue(any('turbo on CPU is unlikely to keep up' in n for n in result['notes']))

    def test_cuda_required_blocks_with_reason(self):
        result = plan(self.route('cuda'), dict(STOCK, whisper=CPU_WHEEL))
        self.assertFalse(result['ready'])
        self.assertTrue(any('CUDA requested' in b and 'CPU-only build' in b for b in result['blockers']))
        self.assertTrue(plan(self.route('cuda'), dict(STOCK, whisper=GPU))['ready'])


if __name__ == '__main__':
    unittest.main()

import asyncio
import json
import math
import os
import platform
import uuid
from base64 import b64encode
from datetime import datetime
from dataclasses import dataclass

from .api import WorkflowInput, WorkflowKind
from .client import Client, ClientEvent, ClientMessage, ClientModels, DeviceInfo, CheckpointInfo
from .client import User
from .image import Extent, ImageCollection
from .network import RequestManager, NetworkError
from .resources import SDVersion
from .settings import PerformanceSettings, settings
from .util import ensure, client_logger as log


@dataclass
class JobInfo:
    local_id: str
    work: WorkflowInput
    remote_id: str | None = None
    worker_id: str | None = None

    def __str__(self):
        return f"Job[{self.work.kind.name}, local={self.local_id}, remote={self.remote_id}]"


class CloudClient(Client):
    default_api_url = os.getenv("INTERSTICE_URL", "https://api.interstice.cloud")
    default_web_url = os.getenv("INTERSTICE_WEB_URL", "https://www.interstice.cloud")

    _requests = RequestManager()
    _queue: asyncio.Queue[JobInfo]
    _token: str = ""
    _user: User | None = None
    _current_job: JobInfo | None = None

    @staticmethod
    async def connect(url: str, access_token: str = ""):
        if not access_token:
            raise ValueError("Authorization missing for cloud endpoint")
        client = CloudClient(url)
        await client.authenticate(access_token)
        return client

    def __init__(self, url: str):
        self.url = url
        self.models = models
        self.device_info = DeviceInfo("Cloud", "Remote GPU", 24)
        self._queue = asyncio.Queue()

    async def _get(self, op: str):
        return await self._requests.get(f"{self.url}/{op}", bearer=self._token)

    async def _post(self, op: str, data: dict):
        return await self._requests.post(f"{self.url}/{op}", data, bearer=self._token)

    async def sign_in(self):
        client_id = str(uuid.uuid4())
        info = f"Generative AI for Krita [Device: {platform.node()}]"
        log.info(f"Sending authorization request for {info} to {self.url}")
        init = await self._post("auth/initiate", dict(client_id=client_id, client_info=info))

        sign_in_url = f"{self.default_web_url}{init['url']}"
        log.info(f"Waiting for completion of authorization at {sign_in_url}")
        yield sign_in_url

        auth_confirm = await self._post("auth/confirm", dict(client_id=client_id))
        time = datetime.now()
        while auth_confirm["status"] == "not-found":
            if (datetime.now() - time).seconds > 300:
                raise TimeoutError("Sign-in attempt timed out after 5 minutes")
            await asyncio.sleep(2)
            auth_confirm = await self._post("auth/confirm", dict(client_id=client_id))

        if auth_confirm["status"] == "authorized":
            self._token = auth_confirm["token"]
            log.info(f"Authorization successful")
            yield self._token
        else:
            error = auth_confirm.get("status", "unexpected response")
            raise RuntimeError(f"Authorization could not be confirmed: {error}")

    async def authenticate(self, token: str):
        if not token:
            raise ValueError("Authorization missing for cloud endpoint")
        self._token = token
        try:
            user_data = await self._get("user")
        except NetworkError as e:
            log.error(f"Couldn't authenticate user account: {e.message}")
            self._token = ""
            if e.status == 401:
                e.message = "The login data is incorrect, please sign in again."
            raise e
        self._user = User(user_data["id"], user_data["name"])
        self._user.images_generated = user_data["images_generated"]
        self._user.credits = user_data["credits"]
        log.info(f"Connected to {self.url}, user: {self._user.id}")
        return self._user

    async def enqueue(self, work: WorkflowInput, front: bool = False):
        work.batch_count = min(work.batch_count, 8)
        if work.models:
            work.models.self_attention_guidance = False
        job = JobInfo(str(uuid.uuid4()), work)
        await self._queue.put(job)
        return job.local_id

    async def listen(self):
        yield ClientMessage(ClientEvent.connected)
        while True:
            try:
                self._current_job = await self._queue.get()
                async for msg in self._process_job(self._current_job):
                    yield msg

            except NetworkError as e:
                msg = self._process_http_error(e)
                log.exception(f"Network error while processing {self._current_job}: {msg}")
                if self._current_job is not None:
                    yield ClientMessage(ClientEvent.error, self._current_job.local_id, error=msg)
            except Exception as e:
                log.exception(f"Unhandled exception while processing {self._current_job}")
                if self._current_job is not None:
                    yield ClientMessage(ClientEvent.error, self._current_job.local_id, error=str(e))
            except asyncio.CancelledError:
                break
            finally:
                self._current_job = None

    async def _process_job(self, job: JobInfo):
        user = ensure(self.user)
        inputs = job.work.to_dict()
        await self._send_images(inputs)
        data = {"input": {"workflow": inputs}}
        response: dict = await self._post("generate", data)

        job.remote_id = response["id"]
        job.worker_id = response["worker_id"]
        cost = _update_user(user, response.get("user"))
        log.info(f"{job} started, cost was {cost}, {user.credits} images remaining")
        yield ClientMessage(ClientEvent.progress, job.local_id, 0)

        while response["status"] == "IN_QUEUE" or response["status"] == "IN_PROGRESS":
            response = await self._post(f"status/{job.worker_id}/{job.remote_id}", {})

            if response["status"] == "IN_QUEUE":
                yield ClientMessage(ClientEvent.queued, job.local_id)

            elif response["status"] == "IN_PROGRESS":
                progress = 0.09
                if output := response.get("output", None):
                    progress = output.get("progress", progress)
                yield ClientMessage(ClientEvent.progress, job.local_id, progress)
            await asyncio.sleep(_poll_interval)

        if response["status"] == "COMPLETED":
            output = response["output"]
            images = await self.receive_images(output["images"])
            pose = output.get("pose", None)
            log.info(f"{job} completed, got {len(images)} images{', got pose' if pose else ''}")
            yield ClientMessage(ClientEvent.finished, job.local_id, 1, images, pose)

        elif response["status"] == "FAILED":
            err_msg, err_trace = _extract_error(response, job.remote_id)
            log.error(f"{job} failed\n{err_msg}\n{err_trace}")
            yield ClientMessage(ClientEvent.error, job.local_id, error=err_msg)

        elif response["status"] == "CANCELLED":
            log.info(f"{job} was cancelled")
            yield ClientMessage(ClientEvent.interrupted, job.local_id)

        elif response["status"] == "TIMED_OUT":
            log.warning(f"{job} timed out")
            yield ClientMessage(ClientEvent.error, job.local_id, error="job timed out")
        else:
            log.warning(f"Got unknown job status {response['status']}")

    async def interrupt(self):
        if self._current_job and self._current_job.remote_id:
            await self._post(f"cancel/{self._current_job.remote_id}", {})

    async def clear_queue(self):
        self._queue = asyncio.Queue()

    @property
    def user(self):
        return self._user

    @property
    def performance_settings(self):
        return PerformanceSettings(
            batch_size=min(4, max(8, settings.batch_size)),
            resolution_multiplier=settings.resolution_multiplier,
            max_pixel_count=min(8, settings.max_pixel_count),
        )

    async def _send_images(self, inputs: dict):
        if image_data := inputs.get("image_data"):
            blob, offsets = image_data["bytes"], image_data["offsets"]
            if _base64_size(len(blob)) < 3_500_000:
                encoded = b64encode(blob).decode("utf-8")
                inputs["image_data"] = {"base64": encoded, "offsets": offsets}
            else:
                s3_object = await self._upload_to_s3(blob)
                inputs["image_data"] = {"s3_object": s3_object, "offsets": offsets}

    async def _upload_to_s3(self, data: bytes):
        upload_info = await self._get("upload")
        log.info(f"Uploading image input to temporary transfer {upload_info['url']}")
        await self._requests.put(upload_info["url"], data)
        return upload_info["object"]

    async def receive_images(self, images: dict):
        offsets = images.get("offsets")
        if not (isinstance(offsets, list) and len(offsets) > 0):
            raise ValueError(f"Could not read result images, invalid offsets: {offsets}")
        if url := images.get("url"):
            log.info(f"Downloading result images from temporary transfer {url}")
            data = await self._requests.download(url)
            return ImageCollection.from_bytes(data, offsets)
        elif b64 := images.get("base64"):
            return ImageCollection.from_base64(b64, offsets)
        else:
            raise ValueError(f"No result images found in server response: {str(images)[:80]}")

    async def compute_cost(
        self, kind: WorkflowKind, sd_version: SDVersion, batch: int, extent: Extent, steps: int
    ):
        op = f"admin/cost/{kind.name}/{sd_version.name}/{batch}/{extent.width}/{extent.height}/{steps}"
        response = await self._get(op)
        return int(response.decode())

    def _process_http_error(self, e: NetworkError):
        message = e.message
        if e.status == 402 and e.data and self.user:  # 402 Payment Required
            try:
                self.user.credits = e.data["credits"]
                message = (
                    f"Insufficient funds - generation would cost {e.data['cost']} tokens. "
                    f"Remaining tokens: {self.user.credits}"
                )
            except:
                log.warning(f"Could not parse 402 error: {e.data}")
        return message


def _extract_error(response: dict, job_id: str | None):
    error = response.get("error", f'"Job {job_id} failed (unknown error)"')
    try:
        error_args = json.loads(error)
        err_msg = error_args.get("error_message", error_args)
        err_trace = error_args.get("error_traceback", "No traceback")
    except Exception:
        err_msg = str(error)
        err_trace = "No traceback"
    return err_msg, err_trace


def _update_user(user: User, response: dict | None):
    if response:
        cost = max(0, user.credits - response["credits"])
        user.images_generated = response["images_generated"]
        user.credits = response["credits"]
        return cost
    else:
        log.warning("Did not receive updated user data from server")
        return 0


def _base64_size(size: int):
    return math.ceil(size / 3) * 4


_poll_interval = 0.5  # seconds

models = ClientModels()
models.checkpoints = {
    "dreamshaper_8.safetensors": CheckpointInfo("dreamshaper_8.safetensors", SDVersion.sd15),
    "realisticVisionV51_v51VAE.safetensors": CheckpointInfo(
        "realisticVisionV51_v51VAE.safetensors", SDVersion.sd15
    ),
    "flat2DAnimerge_v45Sharp.safetensors": CheckpointInfo(
        "flat2DAnimerge_v45Sharp.safetensors", SDVersion.sd15
    ),
    "juggernautXL_version6Rundiffusion.safetensors": CheckpointInfo(
        "juggernautXL_version6Rundiffusion.safetensors", SDVersion.sdxl
    ),
}
models.vae = []
models.loras = []
models.upscalers = [
    "4x_NMKD-Superscale-SP_178000_G.pth",
    "HAT_SRx4_ImageNet-pretrain.pth",
    "OmniSR_X2_DIV2K.safetensors",
    "OmniSR_X3_DIV2K.safetensors",
    "OmniSR_X4_DIV2K.safetensors",
]
# fmt: off
from ai_diffusion.resources import resource_id, ResourceKind, ControlMode, UpscalerName
models.resources = {
    resource_id(ResourceKind.controlnet, SDVersion.sd15, ControlMode.inpaint): "control_v11p_sd15_inpaint_fp16.safetensors",
    resource_id(ResourceKind.controlnet, SDVersion.sd15, ControlMode.scribble): "control_lora_rank128_v11p_sd15_scribble_fp16.safetensors",
    resource_id(ResourceKind.controlnet, SDVersion.sdxl, ControlMode.scribble): "sai_xl_sketch_256lora.safetensors",
    resource_id(ResourceKind.controlnet, SDVersion.sd15, ControlMode.line_art): "control_v11p_sd15_lineart_fp16.safetensors",
    resource_id(ResourceKind.controlnet, SDVersion.sdxl, ControlMode.line_art): "sai_xl_sketch_256lora.safetensors",
    resource_id(ResourceKind.controlnet, SDVersion.sd15, ControlMode.soft_edge): "control_v11p_sd15_softedge_fp16.safetensors",
    resource_id(ResourceKind.controlnet, SDVersion.sd15, ControlMode.canny_edge): "control_v11p_sd15_canny_fp16.safetensors",
    resource_id(ResourceKind.controlnet, SDVersion.sdxl, ControlMode.canny_edge): "sai_xl_canny_256lora.safetensors",
    resource_id(ResourceKind.controlnet, SDVersion.sd15, ControlMode.depth): "control_lora_rank128_v11f1p_sd15_depth_fp16.safetensors",
    resource_id(ResourceKind.controlnet, SDVersion.sdxl, ControlMode.depth): "sai_xl_depth_256lora.safetensors",
    resource_id(ResourceKind.controlnet, SDVersion.sd15, ControlMode.normal): None,
    resource_id(ResourceKind.controlnet, SDVersion.sd15, ControlMode.pose): "control_lora_rank128_v11p_sd15_openpose_fp16.safetensors",
    resource_id(ResourceKind.controlnet, SDVersion.sdxl, ControlMode.pose): "thibaud_xl_openpose_256lora.safetensors",
    resource_id(ResourceKind.controlnet, SDVersion.sd15, ControlMode.segmentation): None,
    resource_id(ResourceKind.controlnet, SDVersion.sd15, ControlMode.blur):"control_lora_rank128_v11f1e_sd15_tile_fp16.safetensors",
    resource_id(ResourceKind.controlnet, SDVersion.sd15, ControlMode.stencil): None,
    resource_id(ResourceKind.controlnet, SDVersion.sd15, ControlMode.hands): None,
    resource_id(ResourceKind.controlnet, SDVersion.sdxl, ControlMode.hands): None,
    resource_id(ResourceKind.ip_adapter, SDVersion.sd15, ControlMode.reference): "ip-adapter_sd15.safetensors",
    resource_id(ResourceKind.ip_adapter, SDVersion.sdxl, ControlMode.reference): "ip-adapter_sdxl_vit-h.safetensors",
    resource_id(ResourceKind.ip_adapter, SDVersion.sd15, ControlMode.face): None,
    resource_id(ResourceKind.ip_adapter, SDVersion.sdxl, ControlMode.face): None,
    resource_id(ResourceKind.clip_vision, SDVersion.all, "ip_adapter"): "clip-vision_vit-h.safetensors",
    resource_id(ResourceKind.lora, SDVersion.sd15, "lcm"): "lcm-lora-sdv1-5.safetensors",
    resource_id(ResourceKind.lora, SDVersion.sdxl, "lcm"): "lcm-lora-sdxl.safetensors",
    resource_id(ResourceKind.lora, SDVersion.sd15, ControlMode.face): None,
    resource_id(ResourceKind.lora, SDVersion.sdxl, ControlMode.face): None,
    resource_id(ResourceKind.upscaler, SDVersion.all, UpscalerName.default): UpscalerName.default.value,
    resource_id(ResourceKind.upscaler, SDVersion.all, UpscalerName.fast_2x): UpscalerName.fast_2x.value,
    resource_id(ResourceKind.upscaler, SDVersion.all, UpscalerName.fast_3x): UpscalerName.fast_3x.value,
    resource_id(ResourceKind.upscaler, SDVersion.all, UpscalerName.fast_4x): UpscalerName.fast_4x.value,
    resource_id(ResourceKind.inpaint, SDVersion.sdxl, "fooocus_head"): "fooocus_inpaint_head.pth",
    resource_id(ResourceKind.inpaint, SDVersion.sdxl, "fooocus_patch"): "inpaint_v26.fooocus.patch",
    resource_id(ResourceKind.inpaint, SDVersion.all, "default"): "MAT_Places512_G_fp16.safetensors",
}
# fmt: on

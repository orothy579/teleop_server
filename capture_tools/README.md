# WidowX 양팔 기록 → 오른팔 pick-and-place → SmolVLA

2026-10-06: **팔 4대 + SLATE odometry + 카메라 3대 통합 수집**은
[MOBILE_CAPTURE.md](MOBILE_CAPTURE.md)를 참조한다. 실제 10초 300프레임 저장·재로딩 검증 완료.
신규 실행 파일은 `mobile_capture.py`이며 기존 `record.py`에는 베이스 수집이 포함되지 않는다.

## 최신: 카메라 3대 수집 검증 (2026-10-02 14:45 KST)

사용자가 CAN 오류 조치 및 정상 조작 가능 상태를 확인한 뒤 20초 실제 기록을 수행했다.
`outputs/record_20261002_144451`에 state/action 각 14차원 598행과 RGB 영상 3개 각각 598프레임 저장.
전체 영상 디코딩, 유한한 상태/명령 값, LeRobot 첫/중간/마지막 프레임 재로딩 검증 통과.
양팔 관절과 양쪽 그리퍼 변화 확인(왼쪽 carriage 약 14.5 mm, 오른쪽 약 22.2 mm).
이번 실행은 CAN 오류 보고 없이 exit_code=0으로 정상 종료했다. 장시간 안정성이나 작업 성공을 검증한 것은 아니다.

| 데이터 키 | RealSense D405 serial |
|---|---|
| camera1 | 260322273312 |
| camera2 | 260522278645 |
| camera3 (추가) | 260322272677 |

```bash
cd /home/midbar/ws/teleop_server
/home/midbar/ws/lerobot_trossen/.venv/bin/python capture_tools/record.py \
  --top-serial 260322273312 \
  --wrist-serial 260522278645 \
  --extra-serial 260322272677 \
  --episodes 1 --seconds 20 --execute

/home/midbar/ws/lerobot_trossen/.venv/bin/python -m teleop_server.check_dataset \
  capture_tools/outputs/record_20261002_144451 --expected-cameras 3
```

`--extra-serial`을 생략하면 기존 2대 수집도 가능하다. `check_cameras.py --expected-count 3`은
팔을 연결하지 않고 3대 동시 영상 수신을 검사한다. 이번 결과의 상세 값은 같은 이름의 `.validation.json` 참조.
학습 시에는 동일한 카메라 키를 유지하고, 기존 2대 데이터와 3대 데이터를 무작정 합치지 않는다.

아래는 최초 2대 구성의 계획 및 이전 검증 이력이다.

2026-10-02 (Asia/Seoul). 첫 예제는 베이스 고정, 오른팔로 큐브 하나를 그릇으로 옮기기.
왼팔은 일정한 자세로 유지하지만 **양팔을 모두 제어·기록**한다. 왼팔을 소프트웨어로 잠그는 구성은 아니다.

## 현재 결과

| 항목 | 확인 결과 |
|---|---|
| 양팔 teleoperation | 사용자 실행 확인. 이번 점검에서는 재구동하지 않음 |
| LeRobot / Trossen | 0.6.0 / trossen-arm 1.11.0 |
| VLA 의존성 | SmolVLA import, train/rollout CLI 로딩 통과 |
| GPU | NVIDIA GB10, PyTorch 2.10.0+cu130, 실제 CUDA 행렬 연산 통과 |
| RealSense | D405 2대 감지, LeRobot API로 두 대 동시 RGB 수신 통과 |
| 카메라 260322273312 | 640×480, 90회 읽기, 새 host capture timestamp 90개, 약 29.98 Hz |
| 카메라 260522278645 | 640×480, 90회 읽기, 새 host capture timestamp 90개, 약 29.34 Hz |
| 카메라 설치 상태 | 사용자가 두 카메라 모두 정상적으로 앞을 보고 있고 수신 영상도 정상이라고 확인. 이전의 천장 방향 해석은 정정함 |
| 저장 경로 | **합성** 60프레임, 2개 H.264 영상, state/action 14차원: 저장·전체 영상 디코딩·LeRobot 재로딩 통과 |
| 실제 proprioception + action 기록 | 10초 299프레임에 이어 12:14:49경부터 20초 598프레임 기록 완료. 양팔 움직임 및 왼쪽 그리퍼 변화 확인. state/action 각각 14차원, 카메라별 영상 598프레임. 파일 검사와 LeRobot 읽기 통과 |
| 실제 시연 / 학습 / 정책 로봇 실행 | 미실행 |
| 베이스 | ROS 2 Jazzy 및 `/home/midbar/interbotix_ws`의 SLATE 드라이버 설치 확인. 현재 조회한 ROS 도메인에서 `/odom` 없음 |

카메라 측정치는 짧은 구간의 host-side 갱신 관찰이며 정확한 센서 간 동기화 보장이 아니다.
장치/버전 결과: `outputs/preflight.json`, 영상 결과: `outputs/cameras/report.json` 및 serial별 PNG.
`outputs/SYNTHETIC_*`는 저장 테스트 전용이며 시연/학습 데이터로 쓰지 않는다.

## 순서와 완료 기준

1. **10/02: 장치와 수집 경로 확인**
   - [x] 사용자 teleoperation 성공 확인
   - [x] RealSense 2대 RGB 동시 수신 및 이미지 저장
   - [x] SmolVLA/RealSense/GPU 의존성 설치 및 기본 검사
   - [x] 합성 데이터로 영상+Parquet 저장/읽기 검증
   - [ ] 카메라 장착, 초점/노출/가림 확인. 물체, 그리퍼, 목표 그릇이 보여야 함
   - [x] 실제 10초 에피소드 1개: 양팔 state/action 14개, RGB 2개, task 저장
   - [x] 실제 Parquet/전체 영상 디코딩/LeRobot 재로딩 검사
   - [ ] 실제 파일 검사와 화면 검토; 좌우 구분, 그리퍼 열림/닫힘, 단위 확인
   - [ ] 실제 루프 속도/카메라 지연/누락 확인. 파일 timestamp 간격만으로 실제 30 Hz라고 판단하지 않음
2. **수집 검증 후: 성공 시연 약 50개**
   - 큐브 1개, 그릇 1개, 고정 조명/카메라/베이스, 오른팔 작업으로 시작
   - 처음 5개를 검토한 뒤 확대. 예: 큐브 시작점 5곳 × 성공 시연 10회
   - 물체를 완전히 놓고 손을 떼는 구간까지 포함. 실패/실수/긴 정지는 다시 기록
   - 관절 순서, 카메라 키/serial, 단위, 작업 문장을 학습·평가에도 동일하게 유지
3. **SmolVLA 학습**
   - 실제 데이터만 사용해 100-step 학습/체크포인트 테스트 → 검증 후 20k-step 후보 학습
   - 학습 시간은 이 GB10에서 측정. 다른 GPU의 시간 수치를 그대로 적용하지 않음
4. **정책 평가**
   - 설치된 0.6.0은 `lerobot-rollout` 사용 (`lerobot-record --policy.path` 예제는 이 환경과 다름)
   - 학습에 쓰지 않은 시작점에서 10회 평가: 성공률, 떨어뜨림, 그리퍼 실패, 실행 지연 기록
   - 시작 자세, 작업 공간, 정지 수단을 확인한 뒤 실행. 추론 주기 측정 후 필요하면 RTC 검토
5. **10/05까지: 베이스 odometry 수집**
   - 문서와 로컬 설치는 **Trossen SLATE** 계열. 실제 베이스가 별도 AgileX Tracer인지 명판 확인 필요
   - 드라이버 실행 환경/ROS_DOMAIN_ID/네임스페이스를 확인하고 실제 odom 토픽 확인
   - `nav_msgs/msg/Odometry`의 header timestamp, frame_id, child_frame_id, pose, twist, covariance 기록
   - 정지 구간과 짧은 직진/회전 구간으로 단위·축·방향·주기·누락 검증
   - 첫 단계는 rosbag2 별도 기록. 이후 카메라/팔과 시간 정렬 구현 및 오차 측정
   - 고정 베이스 pick-and-place의 첫 학습에는 odometry를 넣지 않음

## 설치 재현

추가 20초 기록: `outputs/record_20261002_121434` (2026-10-02 12:14:49경 시작).
598프레임, RGB 영상 2개 각 598프레임, state/action 각각 14차원 검증 통과.
LeRobot 첫/중간/마지막 프레임 재로딩 통과, 네 로봇과 카메라 모두 정상 연결 종료.
양팔 관절 움직임이 실제 값으로 기록됐으며 왼쪽 그리퍼 위치 변화 범위는 약 21.2 mm.
오른쪽 그리퍼는 약 0.0067 mm로 거의 변하지 않았으므로 오른쪽 개폐 검증은 남아 있다.
이 데이터는 조작·수집 검증용이며 pick-and-place 성공 시연 여부는 평가하지 않았다.
세부 값은 같은 이름의 `.validation.json`, 실행 내역은 `.session.json` 참조.

실제 수집 검증 결과는 `outputs/record_20261002_120912`에 저장했다.
같은 이름의 `.session.json`에는 실행 명령과 정상 종료(exit_code=0), `.validation.json`에는
저장/읽기 검사 및 각 관절 변화 범위가 있다. 네 로봇 연결은 모두 정상 종료했다.
이번 기록은 거의 정지 상태였다: action의 팔 관절 최대 변화 약 0.00153 rad,
오른쪽 그리퍼 최대 변화 약 0.000103 m. 따라서 **수집 경로 검증**은 통과했지만,
실제 움직임 추종·그리퍼 개폐·pick-and-place 성공 검증은 남아 있다.
데이터의 task 문장은 기본 pick-and-place 문장이지만 성공 시연으로 사용하지 않는다.
이번 camera1/camera2 대응은 serial 식별용이며 전체/손목 역할은 아직 확정하지 않았다.

```bash
cd /home/midbar/ws/lerobot_trossen
UV_CACHE_DIR=/tmp/lerobot-uv-cache uv sync --locked --extra vla --extra cameras --extra gpu
cd /home/midbar/ws/teleop_server
/home/midbar/ws/lerobot_trossen/.venv/bin/python capture_tools/preflight.py
/home/midbar/ws/lerobot_trossen/.venv/bin/python capture_tools/check_cameras.py
```

장치 접근 가능한 호스트 터미널에서 실행한다. `uv sync`는 LeRobot 환경에서만 실행한다.
수집 도구는 그 환경의 Python을 직접 사용한다.

호환성 메모:

- LeRobot의 `intelrealsense` extra는 `<2.57`로 제한되지만 이 범위에는 현재 ARM64/Python 3.12용 wheel이 없다.
  별도 `cameras` extra로 2.57.7.10387을 고정했다. 설치된 LeRobot의 RGB 카메라 API로 실제 수신까지 검증했다.
  **상류의 선언된 버전 범위 밖**이며 depth 등 전체 기능을 검증한 것은 아니다.
- PyTorch가 GB10 capability 12.1에 대해 빌드 범위가 12.0까지라는 경고를 출력한다. 기본 CUDA 연산은 통과했으나
  SmolVLA 전체 forward/backward 검증은 아직 필요하다.
- `uv pip check`는 `nvidia-cusparselt-cu13`의 플랫폼 경고 1개를 낸다. 다운로드 wheel은 aarch64,
  실제 `.so`도 ARM aarch64지만 내부 WHEEL 태그가 `manylinux2014_sbsa`로 되어 있다. 패키지 메타데이터는 수정하지 않았다.

## 실제 10초 기록

두 카메라를 재배치한 뒤 serial과 역할을 직접 대응시킨다. 현재 감지된 serial은
`260322273312`, `260522278645`이며 어느 쪽이 전체/손목 카메라인지는 아직 확정하지 않았다.

```bash
# 먼저 실행될 명령만 출력한다. 실제 serial 두 개를 넣는다.
/home/midbar/ws/lerobot_trossen/.venv/bin/python capture_tools/record.py \
  --top-serial TOP_SERIAL --wrist-serial WRIST_SERIAL

# 실제 기록: 양팔 모두 시작 자세로 이동하고 teleoperation을 시작한다.
/home/midbar/ws/lerobot_trossen/.venv/bin/python capture_tools/record.py \
  --top-serial TOP_SERIAL --wrist-serial WRIST_SERIAL --execute

# 명령에 표시된 outputs/record_날짜_시간 경로를 사용한다.
/home/midbar/ws/lerobot_trossen/.venv/bin/python -m teleop_server.check_dataset capture_tools/outputs/record_YYYYMMDD_HHMMSS
```

기존 teleoperation 프로세스를 종료하고 리더를 잡을 작업자가 준비된 상태에서 기록한다.
연결 자체가 follower의 `staged_positions` 이동을 수행한다. `Ctrl-C`의 소프트웨어 종료를 비상정지와 동일시하지 않는다.
카메라가 없거나 같은 serial을 두 번 넣으면 래퍼는 로봇 명령 실행 전에 종료한다.
첫 작업 검토 후 `--episodes 5 --seconds 30`으로 늘린다. 실제 50개 수집은 샘플 검토 후 진행한다.

저장 내용:

- `observation.state`: left 7 + right 7 (각 팔 6관절 + 그리퍼). 관절 rad, gripper carriage m
- `action`: 팔로 전달된 관절/그리퍼 위치 명령 14개. 별도 리더 센서 전체 로그와는 다름
- `observation.images.camera1`: 작업대 RGB / `camera2`: 오른팔 집기 부위 RGB
- `task`: `Pick up the cube with the right arm and place it in the bowl.`
- episode/frame/timestamp 및 메타데이터, 별도의 session 시작/종료 wall-clock manifest

속도·effort·external effort는 이 첫 데이터셋에서는 끈다. 위치만 14차원이며, 모든 옵션을 켜면
56차원이 되어 SmolVLA 기본 `max_state_dim=32`를 넘는다. 추가 센서는 별도 로그 또는 상태 선택 전처리를 설계한 후 기록한다.
session wall-clock manifest는 대략적 세션 식별용이며 센서 동기화 기능이 아니다.

## 학습 명령 초안 (실제 시연 확보 후)

```bash
HF_HOME=/home/midbar/ws/teleop_server/capture_tools/outputs/hf_cache \
/home/midbar/ws/lerobot_trossen/.venv/bin/lerobot-train \
  --policy.path=lerobot/smolvla_base --policy.device=cuda --policy.push_to_hub=false \
  --dataset.repo_id=local/widowx_pick_place \
  --dataset.root=/ABSOLUTE/PATH/TO/REAL/DATASET --dataset.video_backend=pyav \
  --batch_size=8 --num_workers=4 --steps=100 --save_freq=100 --eval_freq=0 \
  --output_dir=capture_tools/outputs/train_smoke --wandb.enable=false
```

미실행 초안이다. 데이터 feature와 pretrained config의 영상 키/입출력 차원을 확인한 뒤 실행한다.
테스트 성공 후 새 output_dir로 steps=20000, save_freq=1000 등을 시작점으로 사용한다.
학습 loss만으로 pick-and-place 성공을 판정하지 않는다. 모델/데이터 자동 업로드는 끈 상태다.

## Odometry 확인 명령 (ROS 2 / SLATE)

```bash
source /opt/ros/jazzy/setup.bash
source /home/midbar/interbotix_ws/install/setup.bash
export ROS_LOG_DIR=/tmp/lerobot-ros-logs
ros2 topic list -t
# 실제 토픽명이 /odom인 경우:
ros2 topic info /odom --verbose
ros2 topic echo /odom --once
ros2 topic hz /odom
# 별도 터미널에서, 실제 존재하는 토픽 이름을 사용한다.
ros2 bag record -o /home/midbar/ws/teleop_server/capture_tools/outputs/base_odom_session1 \
  /odom /tf /tf_static /cmd_vel
```

드라이버를 이 점검에서 새로 시작하지 않았다. 베이스 모델/포트와 기존 프로세스를 확인한 후 bringup한다.
기본 도메인에서 토픽이 없다는 결과는 다른 PC/도메인의 드라이버까지 없다는 의미는 아니다.
rosbag metadata에 메시지 수가 실제로 있는지 확인하고 재생하여 pose/twist 변화를 검증해야 완료로 표시한다.

## 참고

- SmolVLA와 약 50개 시연 출발점: https://github.com/huggingface/lerobot/blob/main/docs/source/smolvla.mdx
- RealSense ARM64 wheel: https://pypi.org/project/pyrealsense2/2.57.7.10387/
- PyTorch 공식 wheel: https://download.pytorch.org/whl/cu130
- 사용자 베이스 문서: https://docs.trossenrobotics.com/slate_docs/
- SLATE ROS 2: https://docs.trossenrobotics.com/slate_docs/getting_started/ros_interface/ros2.html

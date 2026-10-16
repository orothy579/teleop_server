# Teleop Monitor 웹 앱

원격 제어와 데이터 수집을 브라우저에서 하는 앱이다. 2026-10-07에 실제 장비로 검증했다.

- 카메라 3대(RealSense D405) RGB 실시간 영상과 depth 미리보기
- 리더·팔로워 4대의 관절 위치
- SLATE 베이스 odometry와 **가상 컨트롤러**(조이스틱·키보드로 `/cmd_vel` 명령)
- 센서별 지연 측정과 상태 표시 (정상 / 주의 / 위험 / 수신 없음)
- **제어 버튼**: 팔 4대 연결과 원격 조작 시작·중지. 팔로워가 리더를 따라간다.
- **녹화 버튼**: RGB와 짝이 맞는 depth, 팔, 베이스를 `mobile_capture.py` 형식에 더해 저장하고 자동 검증한다.
- **녹화 라벨**: 녹화 후 성공·실패, 작업명, 환경명, 작업자, 설명을 입력한다.
- **카메라 배치**: 카메라 1, 2, 3 타일의 위치와 크기를 바꾼다.

녹화는 프로젝트 루트의 `dataset/`에 녹화 시작 시각 이름으로 쌓인다.
DGX 분산 서비스도 같은 `dataset/`에 Jetson에서 받은 세션을 변환해 저장한다.
현재 실행 환경은 `/home/midbar/ws/lerobot_trossen/.venv`를 재사용한다. 코드와 데이터는 이 프로젝트에서 관리한다.

프로젝트 구성: `teleop_server/`는 서버·Jetson edge·녹화/검증 코드, `capture_tools/`는 독립 수집 도구,
`dataset/`은 DGX가 받은 세션이다. `dataset/`, `edge.env`, `.cache/`는 Git에서 제외한다.
현재 DGX와 Jetson의 소스 디렉터리는 배포를 위해 복사한 상태이며 Git 저장소는 아직 만들지 않았다.
이후 같은 저장소를 양쪽 `~/ws/teleop_server`에서 Git으로 동기화하면 된다.
DGX에는 `teleop_jetson` 사본이 필요하지 않다. Jetson의 `~/ws/teleop_jetson`은
가상환경, SLATE 설치, 원본 녹화 파일과 토큰 설정을 보관하는 장비 전용 디렉터리다.

이 앱과 `mobile_capture.py`는 같은 팔과 카메라를 동시에 열 수 없다. 서비스가 켜져 있으면 카메라를 서비스가 쓰고 있다.

## 여러 사용자의 HTTPS 접속

DGX의 `teleop-public` 서비스는 `127.0.0.1:8090`에서 로그인 후 기존 8081 웹앱을 중계한다.
Jetson 수집·업로드 및 내부 정책 제어 경로는 공개 게이트웨이에서 차단한다. 브라우저의
HTTP API와 WebSocket은 각각 로그인 쿠키를 확인하고, 사용자는 계정별로 접속한다.
로그인한 모든 사용자는 로봇 제어 버튼을 사용할 수 있으므로 실제 운전자는 서로 조율한다.

```bash
cd /home/midbar/ws/teleop_server
/home/midbar/ws/lerobot_trossen/.venv/bin/python -m teleop_server.public_gateway add-user alice
/home/midbar/ws/lerobot_trossen/.venv/bin/python -m teleop_server.public_gateway list-users
/home/midbar/ws/lerobot_trossen/.venv/bin/python -m teleop_server.public_gateway remove-user alice
systemctl --user status teleop-public
```

`add-user`는 터미널에서 비밀번호를 숨겨 입력받으며 같은 이름으로 다시 실행하면 비밀번호가
바뀌고 기존 로그인도 무효화된다. 계정·세션 키는 `.public_gateway/`에 권한 0600으로 저장한다.
공개 HTTPS 주소는 DGX의 Tailscale Funnel이 **8090 게이트웨이만** 향한다. 기존 Jetson→DGX
Tailscale 경로와 8081 서비스는 그대로 사용한다. Funnel은 전송 대역폭 제한이 있으므로
여러 명이 3개 카메라 영상을 볼 때 실제 지연·처리량을 확인한다.

## 상시 실행 (systemd 사용자 서비스)

> 2026-10-07 21:03부터 이 두 서비스는 꺼져 있고 자동 시작도 해제됐다. 카메라·팔·베이스가 Jetson으로 옮겨 갔다. 지금은 [Jetson → DGX 분산 모드](#jetson--dgx-분산-모드-distributed)를 쓴다. 아래는 DGX 한 대로 돌릴 때의 설명이다.

두 서비스가 부팅 시 자동으로 시작되고, 죽으면 다시 시작된다. 로그인하지 않아도 시작되도록 linger를 켜 두었다.

| 서비스 | 내용 |
|---|---|
| `teleop-slate` | SLATE 베이스 ROS 드라이버. `/odom` 발행, `/cmd_vel` 구독 |
| `teleop-server` | 웹 서버 `http://<이 PC의 IP>:8080`. 팔은 버튼을 눌러야 연결된다 |

```bash
systemctl --user status teleop-server teleop-slate     # 상태
journalctl --user -u teleop-server -f                  # 로그
systemctl --user restart teleop-server                 # 재시작 (팔이 연결돼 있으면 수면 자세로 돌아간 뒤)
systemctl --user stop teleop-server teleop-slate       # 정지. mobile_capture.py를 쓰려면 먼저 정지
bash teleop_server/deploy/install.sh                   # 다시 설치 (서비스 파일 복사, 활성화)
```

- 서비스를 멈추면 SIGINT로 정상 종료한다. 베이스 속도 0, 진행 중 녹화를 실패로 표시해 저장, 팔 정상 해제 순서다. 최대 5분 기다린다.
- SLATE 드라이버를 손으로 띄우지 않는다. 서비스와 두 개가 같은 시리얼 포트를 잡으면 충돌한다.
- 서비스 실행 스크립트는 `deploy/run_server.sh`, `deploy/run_slate_driver.sh`이다. 서버 옵션을 바꾸려면 `run_server.sh`를 고친 뒤 재시작한다.

직접 실행할 때는 서비스를 멈춘 뒤 아래처럼 한다.

```bash
cd /home/midbar/ws/teleop_server
source /opt/ros/jazzy/setup.bash
export ROS_LOG_DIR=/tmp/lerobot-ros-logs
/home/midbar/ws/lerobot_trossen/.venv/bin/python -m teleop_server.server --arms teleop
```

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--cameras` | `realsense` | `fake`는 합성 영상, `off`는 끔 |
| `--serials` | 기존 수집기와 같은 3개 | camera1/2/3 순서 |
| `--no-depth` | depth 켜짐 | depth 수집·저장을 끈다 |
| `--depth-min`, `--depth-max` | 0.05, 6.5535 m | 저장 범위. 최소값 이하는 "측정 없음" |
| `--depth-preset` | `ultrafast` | depth 무손실 압축 속도 설정 |
| `--arms` | `off` | `teleop`은 실제 팔 4대를 제어 버튼으로 연결. `fake`는 시뮬레이션 |
| `--arm-rate` | 30 | 팔 루프 주기(Hz). 녹화하려면 `--fps`와 같아야 함 |
| `--odom` | `ros` | `fake` 또는 `off`. `off`이면 녹화 불가 |
| `--base-control` | `auto` | 가상 컨트롤러. `ros`는 `--cmd-topic`에 발행, `fake`, `off` |
| `--base-max-linear`, `--base-max-angular` | 0.3 m/s, 0.6 rad/s | 브라우저 명령 상한. 드라이버 한계는 1.0 |
| `--task` | 기존 수집기 기본 문장 | 녹화 작업 문장 기본값 |
| `--record-dir` | `dataset/` | 녹화 세션 폴더 위치. 합성 센서를 쓰면 기본값이 `/tmp/teleop_server_synthetic` |
| `--no-validate` | 꺼짐 | 녹화 후 자동 검증 생략 |
| `--host`, `--port` | `0.0.0.0`, 8080 | |

하드웨어 없이 화면만 볼 때는 `--cameras fake --arms fake --odom fake`를 쓴다. 이때 가상 컨트롤러는 합성 베이스를 움직인다.

## 제어와 안전

- 서버를 켜기만 해서는 팔이 연결되지 않는다.
- **제어 시작**: 확인 창 후 팔 4대가 준비 자세로 이동하고 팔로워가 리더를 따라간다. 기존 lerobot 클래스의 `connect()`를 그대로 쓴다. 카메라가 첫 프레임을 보내기 전에는 시작할 수 없다.
- **제어 중지**: 녹화 중이면 먼저 저장하고, 베이스 조작도 끈다. 그다음 팔이 준비 자세를 거쳐 수면 자세로 돌아간다. 약 18초 걸린다.
- 팔 읽기 오류가 나면 루프를 멈추고 이동 없이 드라이버 정리만 한다.
- **페이지에 접속할 수 있는 사람은 누구나 팔과 베이스를 움직일 수 있다.** 같은 네트워크에서만 쓰거나 `--host 127.0.0.1`로 제한한다.
- 소프트웨어 종료는 비상정지가 아니다.

### 가상 베이스 컨트롤러

"베이스 조작 켜기"를 누르고 확인해야 명령이 나간다. 조이스틱을 누르고 있거나 키를 누르고 있는 동안만 움직인다.

| 입력 | 동작 |
|---|---|
| 조이스틱 위·아래 | 전진·후진 (`linear.x`) |
| 조이스틱 왼쪽·오른쪽 | 좌회전·우회전 (`angular.z`) |
| W·S 또는 ↑·↓ | 전진·후진 |
| A·D 또는 ←·→ | 좌회전·우회전 |
| 속도 슬라이더 | 상한의 10~100% |
| 정지 | 즉시 0, 조작 끔 |

- 손을 떼면 즉시 0을 보낸다. 서버는 명령이 0.25초 끊기면 0을 보내고 발행을 멈춘다. 드라이버도 0.3초 후 멈춘다.
- 조작하던 브라우저의 연결이 끊기거나, 탭이 가려지거나, 창이 포커스를 잃으면 즉시 정지한다.
- 한 번에 한 사용자만 조작한다. 입력 칸에 글을 쓰는 동안에는 키보드 조작이 동작하지 않는다.
- 조작하지 않을 때는 `/cmd_vel`에 아무것도 발행하지 않는다. 다른 조종 장치와 충돌하지 않는다.
- **실제 베이스를 움직이는 시험은 하지 않았다.** 처음 쓸 때는 주변을 비우고 속도 슬라이더를 낮춰 시험한다.

## 녹화

**녹화 시작**은 제어 중일 때만 켜진다. 녹화 하나가 세션 폴더 하나이고 에피소드 하나다.

```
teleop_server/dataset/
  20261007_180915/          녹화 시작 시각(KST). 이름순 = 시간순
    dataset/                LeRobot 데이터셋 (mobile_capture.py 세션과 같은 구조)
    base_raw.jsonl
    session.json
    validation.json
  20261007_181502/
  20261007_181502_2/        같은 초에 또 녹화하면 _2, _3 ...
```

- `dataset/`은 `.gitignore`에 들어 있어 git에 올라가지 않는다.
- 합성 센서로 녹화하면 `/tmp/teleop_server_synthetic`에 `_SYNTHETIC`이 붙은 이름으로 저장된다. 학습 데이터 폴더에 섞이지 않는다.
- 독립 실행 수집기 `teleop_server/mobile_capture.py`는 `capture_tools/outputs`에 저장한다.

`mobile_capture.py`의 특징 정의, base 구독, 프레임 구성 함수를 그대로 쓰고 아래 항목을 더한다.

| 파일·항목 | 내용 |
|---|---|
| `<시각>/dataset/` | LeRobot v3.0. state·action·leader_state 각 14, base 13, base 명령 6, 시각 정보 |
| `observation.images.cameraN` | RGB 640×480, 30 fps, H.264 |
| `observation.images.cameraN_depth` | depth 640×480, RGB 픽셀 격자에 정렬, 미터 단위, HEVC 무손실 12비트 로그 양자화 |
| `observation.camera_depth_dt_ms` | 프레임별 RGB와 depth의 촬영 시각 차이 |
| `observation.camera_frame_number` | 프레임별 카메라 장치 프레임 번호 |
| `base_raw.jsonl` | 녹화 중 받은 모든 `/odom`, `/cmd_vel` 원본 |
| `session.json` | 작업 문장, 상태, 프레임 수, 카메라 렌즈 파라미터, depth 설정, 품질 지표, 라벨 |
| `validation.json` | 검증 결과 |
| `dataset/meta/episode_labels.json` | 라벨 사본 |

- **베이스 정보**: odometry 13개 값과 `/cmd_vel` 명령이 모든 프레임에 함께 저장된다. 가상 컨트롤러로 보낸 명령도 여기에 기록된다.
- **depth 짝**: RGB와 depth는 같은 RealSense 묶음에서 가져온다. 시각 차이가 2 ms를 넘는 묶음은 짝으로 쓰지 않고, 직전의 확인된 짝을 다시 쓴다. 실측 차이는 0.305 ms다.
- **depth 정렬**: D405의 depth는 RGB와 렌즈 파라미터가 다르고 약 0.4도 돌아가 있다. 그래서 RGB 격자로 다시 투영해 픽셀이 1:1로 대응한다. 렌즈 파라미터는 `session.json`의 `cameras`에 있다.
- **depth 정밀도**: 0.05 m 이하는 측정 없음이다. 양자화 오차는 거리의 0.06% 이하다. 0.3 m에서 0.18 mm이고, 압축 자체는 무손실이다.
- **depth 읽기**: `LeRobotDataset(..., depth_output_unit="m")`로 읽으면 미터 단위 `(1, 480, 640)` 텐서가 나온다.
- **녹화 조건**: 팔 제어 중이어야 한다. 카메라 3대가 0.5초 안의 RGB와 depth 짝을 갖고 있어야 한다. odometry가 0.25초 안에 들어와야 하고, 최근 짝 불일치가 10% 이하여야 한다.
- **실패 처리**: 녹화 중 카메라나 odometry가 기준을 넘기면 녹화만 멈추고 실패로 표시한다. 그때까지의 프레임은 저장된다.
- **품질 지표**: 카메라 최대 나이, 반복 저장된 프레임 수, RGB와 depth의 최대 시각 차이, 제외된 불일치 묶음 수를 남긴다.
- **자동 검증**: 저장 후 `python -m teleop_server.validate <세션>`을 별도 프로세스로 실행한다. 기존 `check_mobile.py` 검사에 depth 프레임 수, 값 범위, 짝 시각 차이 검사를 더했다.
- **용량과 시간**: 5초 녹화가 약 68 MB였다. 5분이면 약 4 GB다. 녹화 중에는 임시 이미지가 카메라당 초당 약 48 MB 쌓이고, 저장 때 인코딩한다.

### 녹화 라벨

녹화가 끝나면 "녹화 라벨" 칸에 그 세션이 자동으로 선택된다. 결과(성공·실패)는 필수다. 작업명, 환경명, 작업자, 설명을 입력하고 "라벨 저장"을 누른다.

- 세션 목록에서 이전 녹화를 골라 라벨을 달거나 고칠 수 있다. 고칠 때마다 수정 횟수가 올라간다.
- 환경명과 작업자는 이 브라우저에서 마지막으로 쓴 값이 기본으로 채워진다.
- 데이터셋 안의 작업 문장(task)은 녹화 시작 때 입력한 문장 그대로다. 라벨의 작업명은 별도 항목이다.
- 녹화 중이거나 저장 중인 세션에는 라벨을 저장할 수 없다.

### API

```bash
curl -X POST localhost:8080/api/control -H 'Content-Type: application/json' -d '{"action": "start"}'
curl -X POST localhost:8080/api/record  -H 'Content-Type: application/json' -d '{"action": "start", "task": "..."}'
curl -X POST localhost:8080/api/record  -H 'Content-Type: application/json' -d '{"action": "stop"}'
curl localhost:8080/api/sessions
curl -X POST localhost:8080/api/label   -H 'Content-Type: application/json' \
  -d '{"session": "20261007_180915", "outcome": "success", "task_name": "...", "environment": "...", "operator": "...", "notes": "..."}'
curl -X POST localhost:8080/api/base    -H 'Content-Type: application/json' -d '{"action": "disable"}'
curl -X POST localhost:8080/api/control -H 'Content-Type: application/json' -d '{"action": "stop"}'
```

베이스 속도 명령은 웹소켓 메시지로만 받는다. 데드맨 동작을 보장하기 위해서다.

## 카메라 배치

카메라 칸 오른쪽 위의 도구로 배치를 바꾼다. 배치는 이 브라우저에 저장되어, 새로고침하거나 서버가 재시작돼도 유지된다. 다른 PC의 브라우저는 각자 배치를 가진다.

| 도구 | 동작 |
|---|---|
| 배치 편집 | 켜면 타일을 끌어서 옮기고, 오른쪽 아래 손잡이로 크기를 바꾼다. 영상 비율 4:3은 유지된다 |
| 키보드 (편집 중) | 타일을 클릭하거나 Tab으로 고른 뒤 화살표로 1% 이동, Shift+화살표로 5%, `+` `-`로 크기 |
| 배치 선택 | 가로 3분할, 카메라 1·2·3 크게, 세로로 쌓기 |
| 기본 배치 | 처음 배치로 되돌린다 |

- 위치와 크기는 카메라 칸 너비에 대한 비율로 저장된다. 창 크기가 바뀌어도 같은 모양이 된다.
- 타일은 서로 겹칠 수 있다. 마지막에 움직인 타일이 위에 보인다.
- 화면 폭이 900px 이하이면 세로로 쌓이고, 배치 편집은 꺼진다.

## 지연 측정

모든 시각은 서버 PC의 시계(unix ns)로 맞춘다. 브라우저는 핑으로 시계 차이를 추정한다. 다른 PC의 브라우저에서도 종단 지연이 맞게 계산된다.

| 구간 | 카메라 | 팔 | odometry |
|---|---|---|---|
| 센서 | 노출 중앙 → 서버 수신 | 컨트롤러 출력 시각 → 서버 수신 (하한값) | 메시지 헤더 시각 → 서버 수신 |
| 서버 | 수신 → 웹소켓 전송 (JPEG 인코딩 포함) | 수신 → 전송 | 수신 → 전송 |
| 네트워크 | 전송 → 브라우저 수신 | 같음 | 같음 |
| 렌더 | 수신 → 디코딩·그리기 완료 | 수신 → 다음 화면 갱신 | 같음 |
| 종단 | 노출 중앙 → 화면 표시 | 컨트롤러 시각 → 화면 표시 | 헤더 시각 → 화면 표시 |

- 상태 기준: 종단 p95가 100 ms 미만이면 정상, 200 ms 미만이면 주의, 그 이상이거나 1초 이상 수신이 없으면 위험이다.
- 카메라 타일에는 최근 depth 짝 불일치 비율이 2%를 넘으면 경고가 붙는다.
- 브라우저는 센서 메시지마다 수신 확인을 보내고, 서버는 스트림별로 미확인 메시지를 3개까지만 보낸다. 느린 연결에서도 지연이 쌓이지 않는다.

## Jetson → DGX 분산 모드 (`distributed/`)

Jetson(`teleop-jetson` 서비스, `edge.py`)이 카메라·팔·베이스를 열고 제어 루프를 돌리며, 녹화도 Jetson 디스크에 한다. DGX(`teleop-dgx` 서비스, `server.py`, 포트 8081)는 실시간 화면을 브라우저에 보여 주고, 녹화가 끝난 세션을 받아 `dataset/`에 LeRobot 형식으로 변환한다. 두 장비는 같은 Wi-Fi로 연결하고 유선은 쓰지 않는다.

- 연결은 웹소켓 2개와 HTTP 업로드다. `/edge/control`은 명령, 시계 보정 핑(0.2초마다), 연결 감시를 맡는다. `/edge/data`는 실시간 화면용 패킷을 보낸다(RGB JPEG q85는 30 Hz, depth 무손실 u16 행 차분 + lz4는 10 Hz). DGX는 받은 JPEG를 재압축하지 않고 브라우저에 보낸다. `/edge/upload/...`는 녹화가 끝난 세션 파일을 받는다.
- 실시간 RGB 화면은 평소 640×480, 30 Hz로 보낸다. 녹화 중에는 Jetson에서 **화면 전송용 RGB만** 320×240으로 줄이고 30 Hz를 목표로 한다. 카메라 writer 대기열이 16프레임 이상 쌓이면 녹화 누락을 막기 위해 화면 전송을 10 Hz로 낮추고, 대기열이 4프레임 이하로 내려가면 복원한다. 업로드 중 화면은 10 Hz다. 화면 전송 주기는 팔 제어 주기와 별도로 실행한다. 깊이 화면은 최대 10 Hz이고, Jetson의 로컬 녹화에는 매 주기의 원본 640×480 RGB/depth를 저장한다. 무선 구간이 혼잡하면 최신 화면을 우선하고 오래된 전송 대기 프레임은 버린다.
- `curl :8081/api/edge`의 `transport`에서 압축 시간, 큐, 미확인 패킷 수, 버린 주기 수를 본다.

### 연결 끊김 기준 (2026-10-08부터 3초)

- 로봇 제어가 멈추는 경우는 **제어 링크가 3초 넘게 조용할 때뿐이다**(DGX와 Jetson이 0.2초마다 주고받는 자동 메시지 기준, 사람의 조작과 무관). 이때 Jetson은 팔 제어를 멈추고 베이스를 끈다. DGX도 같은 3초에 세션을 닫는다. 팔 원격조작은 Jetson 안에서 리더 → 팔로워로 돌아가므로, 이보다 짧은 Wi-Fi 끊김에서는 계속된다.
- 베이스는 별도로, 조이스틱 명령이 0.25초 끊기면 바로 멈춘다. 링크 기준과 상관없다.
- 늦게 도착한 명령: 움직임을 시작하는 명령(제어 시작, 베이스 켜기, 베이스 이동, 녹화 시작)은 0.3초 넘게 늦으면 버린다. 멈추는 명령(제어 중지, 베이스 끄기, 녹화 중지)은 늦어도 실행한다.
- 카메라 데이터 링크만 끊기면 그 링크만 다시 연결한다. 로봇 제어는 계속된다.
- 0.5초가 넘는 제어 링크 공백은 양쪽 로그에 남는다(`journalctl --user -u teleop-dgx`, Jetson은 `journalctl _SYSTEMD_USER_UNIT=teleop-jetson.service`). DGX 로그에는 어느 쪽이 늦었는지도 적힌다. 누적값은 `curl :8081/api/edge`의 `link`(Jetson 쪽은 `link.jetson`)에서 본다.
- 2026-10-08 15:48 실제로 끊긴 원인은 Jetson → DGX 제어 메시지가 1초 넘게 늦은 것이었다(당시 기준 1초). 두 장비 모두 Wi-Fi 절전이 켜져 있었다(Jetson: NetworkManager `wifi.powersave = 3`, Realtek 드라이버 `rtw_power_mgnt=2`). 절전 끄기는 아래 명령으로 한다(sudo 필요).

```bash
# Jetson (즉시 + 재부팅 후 유지)
sudo iw dev wlP1p1s0 set power_save off
printf '[connection]\nwifi.powersave = 2\n' | sudo tee /etc/NetworkManager/conf.d/zz-teleop-wifi-powersave-off.conf
printf 'options rtl8822ce rtw_power_mgnt=0 rtw_ips_mode=0\n' | sudo tee /etc/modprobe.d/rtl8822ce-teleop.conf
sudo nmcli general reload conf
# DGX
sudo iw dev wlP9s9 set power_save off
printf '[connection]\nwifi.powersave = 2\n' | sudo tee /etc/NetworkManager/conf.d/zz-teleop-wifi-powersave-off.conf
sudo nmcli general reload conf
# 확인: 둘 다 "Power save: off"
iw dev wlP1p1s0 get power_save   # DGX는 wlP9s9
```

### AI 제어 (2026-10-13)

제어 카드의 **AI 제어…**는 `/home/midbar/ws/inference/`에서 `inference.yaml`이 있는 모델 폴더를 찾아 목록으로 보여 준다.

- 모델을 **모델 로드**로 한 번 불러 두면, 그다음부터 **AI 제어 시작**과 **AI 정지**가 바로 된다.
- 워커는 systemd 사용자 서비스 `teleop-inference-<모델>`로 실행되므로, 이 서버를 다시 시작해도 모델이 메모리에 남는다.
- AI가 제어하는 동안:
  - 화면 위에 파란 **AI 제어 중** 띠(경과 시간·chunk·추론 시간·작업 문장, AI 정지 버튼)가 계속 보인다.
  - 로봇 제어 상태는 "AI가 제어 중"으로 표시된다.
  - 팔 관절 표에는 팔로워만 보인다. 리더는 쓰지 않으므로 리더 값과 차이는 숨긴다.
- 입력(카메라·팔 상태)이 잠깐 늦으면 마지막 목표 자세를 유지하며 기다린다. 이때 띠가 노란색 "⚠ 입력 지연"으로 바뀐다. `observation.max_stall_s`(1.5초)를 넘으면 오류로 끝난다.
- 끝나면 결과(끝·정지함, 시간, chunk, 입력 지연 횟수)를 1분 동안, 또는 팔이 자세를 유지하는 동안 보여 준다.
- **AI가 끝난 뒤: AI 재개 또는 원격조작 전환**(2026-10-13): AI가 끝나면 팔로워는 그 자세를 유지한다(hold). 위쪽 띠와 제어 카드에서 둘 중 하나를 고른다.
  - **AI 재개**: 같은 모델·작업 문장·실행 시간으로 지금 자세에서 다시 실행한다. 확인 창을 한 번 거친다.
  - **원격조작 전환**: 팔 4대가 준비 자세로 돌아간 뒤 원격조작으로 바뀐다. 과정은 아래와 같다.
  - AI 제어 창의 "끝나면 바로 원격조작으로 전환"(기본 꺼짐)을 켜면, 시간이 끝나거나 오류로 끝났을 때 고르지 않고 바로 전환한다. **AI 정지**로 멈춘 경우에는 이 설정과 상관없이 자세를 유지한다.

  원격조작 전환 과정은 다음과 같다.
  - 리더 2대와 팔로워 2대가 함께 준비 자세(staged, "제어 시작" 직후 자세)로 부드럽게 이동한다.
    - 2~8초 걸린다. 가장 멀리 있는 관절 기준 평균 0.5 rad/s, 최대 0.75 rad/s다.
    - 이동하는 동안 리더는 위치 제어를 받으므로 손으로 움직여지지 않는다.
  - 도착하면 리더는 다시 중력 보상(손으로 움직임)으로 바뀌고, 팔로워는 리더를 따라가며, 베이스 조이스틱도 다시 켜진다.
  - "제어 중지 → 제어 시작"과 같은 상태가 되지만, 수면 자세와 재연결(약 20초 이상)을 거치지 않는다.
  - 돌아가는 동안에는 AI 제어를 시작할 수 없다. 멈추려면 **제어 중지**를 누른다.
  - API는 `POST /api/control {action: "return"}`(Jetson 명령 `arms_return`)이다. Jetson 쪽 코드는 `sources/arms.py`의 `ReturnMotion`이다.
- **오류 알림**: 다음 경우 화면 위에 빨간 "⚠" 띠가 뜬다.
  - 실행이 오류로 끝났을 때
  - 모델 로딩에 실패했을 때
  - 워커가 비정상 종료했을 때

  띠는 **확인**을 누를 때까지 남는다. 처음 뜰 때 경고음이 울리고 탭 제목 앞에 "⚠ AI 오류"가 붙는다.
- **오류 기록**: 오류마다 `<모델 폴더>/outputs/errors/ai_error_<시각>_<run|load|crash>.json`에 한 파일로 저장한다. 내용은 다음과 같다.
  - 오류 메시지와 traceback
  - 실행 정보, 실행 로그의 설정과 마지막 30개 이벤트
  - 워커 로그 마지막 200줄
  - 오류를 감지한 시점의 로봇·센서 상태(연결, 센서별 Hz·지연, 팔 정책 상태)

  빨간 띠의 **오류 기록 받기**와 AI 제어 창의 **오류 기록** 목록에서 내려받는다. 한 오류는 페이지를 새로 고치거나 서버를 다시 시작해도 한 번만 저장된다.
- 실행 로그(`outputs/robot_run_*.jsonl`)는 창의 **실행 기록**(최근 100개)에서 받는다. 오류로 끝난 실행은 마지막 `end` 이벤트에 traceback이 있다.
- **고른 로그만 받기**: 실행 기록과 오류 기록(최근 50개)의 체크박스로 고르거나 목록별 "전체 선택"을 쓴 뒤 **선택한 로그 받기 (.zip)**를 누른다.
  - zip 안에는 `<모델>_ai_logs_<시각>/` 폴더가 있고, 그 아래 `runs/`(실행 로그), `errors/`(오류 보고서), `index.json`이 들어간다.
  - `index.json`에는 실행마다 결과·시간·chunk·입력 지연·오류가, 오류마다 종류·메시지가 들어 있다.
  - 한 번에 300개까지 받을 수 있다. 다른 모델을 고르면 선택이 지워진다.
- **제어 중지**는 AI 실행을 먼저 끝낸다.
- API:
  - `GET /api/ai`
  - `POST /api/ai {action: load|unload|run|stop|logs|runs}`
  - `GET /api/ai/runs/<모델>/<파일>`
  - `GET /api/ai/errors/<모델>/<파일>`
  - `GET /api/ai/bundle/<모델>?run=<파일>&run=…&error=<파일>…` (zip)

  코드는 `ai_control.py`와 `static/ai.js`에 있다.
- 모델 추가 방법과 워커 API는 `/home/midbar/ws/inference/README.md`에 있다.

### 데이터셋 목록 (2026-10-12)

상단의 **데이터셋 목록** 버튼을 누르면 `dataset/`에 있는 모든 세션이 팝업으로 뜬다.

- **목록**: 녹화 시작 시각, 길이, 프레임, 녹화 상태, 결과 라벨, 작업명, 작업자, 환경. 검색(세션·작업명·작업자·환경·설명)과 보기(성공/실패/라벨 미입력/녹화 실패) 필터가 있다. 행마다 보기·라벨 수정·삭제 버튼이 있다.
- **세션 보기**(행 클릭):
  - 녹화 정보와 품질: 길이, 측정 Hz, 누락 주기, 카메라 최대 지연, 반복 프레임, RGB-depth 시각차.
  - 라벨.
  - 카메라 3대 영상을 같이 재생하고, 슬라이더로 프레임을 옮긴다. Jetson 원본 mp4가 없으면 LeRobot 영상을 쓴다.
  - depth: 저장된 원본(카메라당 프레임마다 PNG 1장, 30 fps)을 미리보기 영상으로 만들어 RGB와 같이 30 fps로 재생한다(밝을수록 가까움, 0~1 m, 검정 = 측정 없음).
    - 세션을 처음 열 때 DGX가 만들고(19초 세션에 약 7초) `~/.cache/teleop_server/depth_preview/<세션>/`에 저장한다.
    - 이 캐시는 데이터셋 폴더 밖에 있어 보내기에 포함되지 않고, 세션을 지우면 함께 지워진다.
    - 만드는 동안은 프레임별 이미지를 보여 준다.
    - 멈춘 상태에서는 그 프레임의 측정 비율과 중앙값 거리가 표시된다.
  - 팔로워 관절·그리퍼·베이스 궤적 차트. 세로선이 현재 프레임이고, 마우스를 올리면 값이 나오며, 누르면 그 시점으로 이동한다.
  - 현재 프레임 값 표와 저장 용량.
- **라벨 수정**은 기존 라벨 팝업을 연다. 작업명을 바꾸면 데이터셋 task도 바뀐다. 메인 화면의 "라벨 보기·수정" 버튼은 이 목록으로 대체되어 없앴다. 녹화를 멈추면 뜨는 라벨 팝업은 그대로다.
- **삭제**는 확인 후 세션 폴더를 `dataset/.trash/`로 옮긴다. 그 폴더를 비우기 전까지는 되살릴 수 있다. 녹화·저장 중인 세션은 지울 수 없다.
- API: `GET /api/datasets`, `GET /api/datasets/<세션>`, `DELETE /api/datasets/<세션>`, `GET /api/datasets/<세션>/video/<camera>`(Range 지원), `GET /api/datasets/<세션>/depth/<camera>/<프레임>`(JPEG). 코드는 `teleop_server/datasets.py`와 `static/datasets.js`에 있다.

### 데이터셋 보내기 (SSH, 2026-10-12)

데이터셋 목록에서 세션 왼쪽 칸을 체크한 뒤 **보내기…** 또는 **<대상>(으)로 바로 보내기**를 누른다.

- 받는 컴퓨터: 호스트·포트·사용자·받을 폴더(기본 `teleop_datasets`, 받는 쪽 홈 기준)를 입력하거나, ssh 설정(`Host … User … Hostname … Port …`)을 붙여넣어 채운다. **대상 저장**을 누르면 `~/.config/teleop_server/transfer_targets.json`에 저장된다(비밀번호는 저장하지 않음). 가장 최근에 저장한 대상이 "바로 보내기" 버튼이 된다.
- **받을 폴더**(데이터셋이 저장될 경로)는 직접 입력하거나 **찾아보기…**로 고른다.
  - 찾아보기는 받는 컴퓨터에 SSH로 연결해 폴더를 보여 준다. 위로·홈·경로 입력으로 이동하고, 새 폴더를 만들 수 있다.
  - 쓰기 가능 여부, 여유 공간, 그 폴더에 이미 있는 녹화 세션 수가 표시된다.
  - **이 폴더에 저장**을 누르면 받을 폴더 칸이 채워진다. **대상 저장**을 누르면 다음에도 그 경로를 쓴다.
- 비밀번호는 연결할 때 한 번 입력한다. 찾아보기·연결 확인으로 열린 연결은 전송까지 그대로 쓰므로 다시 입력하지 않는다. 연결은 5분 동안 쓰지 않거나 팝업을 닫으면 닫힌다.
- DGX는 비밀번호를 SSH 마스터 연결을 한 번 여는 데만 쓰고 저장하지 않는다. 브라우저 → DGX 구간은 HTTP라서 같은 Wi-Fi 안에서는 암호화되지 않는다.
- 세션마다 tar로 `받을폴더/.incoming/<세션>`에 보낸다. 모든 파일의 SHA-256을 원본과 비교한 뒤에만 `받을폴더/<세션>`으로 옮긴다. 받는 쪽에 같은 세션이 이미 있으면 건너뛰고, 받는 쪽 데이터는 덮어쓰거나 지우지 않는다. 받는 쪽에는 tar·sha256sum·find·xargs만 있으면 된다(rsync 불필요).
- 진행 상황(세션별 상태, 속도, 남은 시간)은 팝업과 상단 배지에 보인다. 팝업을 닫아도 전송은 계속되며, **전송 취소**로 멈출 수 있다. 보내는 중인 세션은 삭제할 수 없다.
- 업로드 속도 제한 기본값은 100 Mbit/s(0 = 제한 없음)다. DGX Wi-Fi가 Jetson 연결과 같으므로 로봇을 조작하는 동안에는 제한을 둔다.
- 2026-10-12 bai-vscode(dropbear SSH, Ubuntu 24.04)로 시험했다. 60 MB 시험 세션이 로그인과 검증을 포함해 1.8초(약 266 Mbit/s)에 도착했다. 시험 폴더는 지웠다.

### SLATE(베이스) 전원과 odometry (2026-10-12)

- SLATE 전원을 끄면 USB 시리얼(`/dev/slate`)도 사라진다. 다시 켜면 새 번호(`/dev/ttyUSB1` 등)로 생긴다. 예전 드라이버는 사라진 포트를 붙잡은 채 계속 "실행 중"으로 남아 odometry를 보내지 않았다.
- 이제 `run_slate.sh`가 1초마다 드라이버가 연 포트를 확인한다. 포트가 사라지면 드라이버를 끝내고, systemd가 다시 시작한다. 새 드라이버는 SLATE가 켜질 때까지 몇 초마다 다시 시도하고, 켜지면 포트를 스스로 찾는다.
- 웹의 "모바일 베이스" 제목 옆에 odometry 상태와 **SLATE 드라이버 재시작** 버튼이 있다. odometry가 끊기면 떠 있는 컨트롤러에도 같은 버튼이 나타난다. 버튼은 Jetson에서 `systemctl --user restart teleop-jetson-slate`를 실행한다. 실제 장비에서 누른 뒤 2.6초 만에 odometry가 돌아왔다(2026-10-12). 재시작 후에는 odometry 위치(x, y, yaw)가 0부터 다시 시작될 수 있으니 녹화 사이에 누른다.
- odometry가 없으면 녹화를 시작하지 않는다. 녹화 중에 끊기면 "지금 저장되지 않고 있습니다"가 빨간 글씨로 표시된다.

### Jetson 로컬 녹화 (2026-10-08부터)

녹화는 Wi-Fi에 의존하지 않는다. 녹화 중 Wi-Fi가 끊기면 팔 제어는 안전을 위해 멈추지만 녹화는 계속되고, 제어가 멈춘 주기 수와 이벤트가 기록된다. 녹화는 녹화 종료 또는 제어 종료 버튼으로만 끝난다.

1. **녹화 중 (Jetson)**: 팔 제어 주기(30 Hz, Jetson 시계)마다 한 프레임을 쓴다. 카메라마다 RGB는 H.264 mp4(640×480, libx264 superfast crf 18)로, depth는 카메라 원본 uint16을 무손실 압축(u16 행 차분 + lz4)해 쓴다. 팔·베이스·카메라 메타데이터는 `cycles.jsonl`에, 베이스 원본 메시지는 `base_raw.jsonl`에 쓴다. 저장 위치는 `~/ws/teleop_jetson/recordings/<세션>/`이다.
2. **마무리**: 파일을 닫고 SHA-256을 계산한다(60초 녹화에 약 4초).
3. **전송**: Jetson이 파일마다 크기와 SHA-256을 붙여 DGX로 올린다. 제어 링크를 위해 200 Mbit/s로 제한한다(`--upload-mbps`). DGX가 모든 파일을 확인한 뒤에만 Jetson 사본을 지운다. 실패하면 파일을 남기고 5초마다 다시 보낸다.
4. **변환·검증 (DGX)**: 기존 녹화와 같은 세션 형식으로 바꾸고 검증한다. 성공하면 `dataset/.incoming/`의 원본을 지우고, 실패하면 남긴다. DGX가 다시 시작되면 `.incoming`에 확인된 세션을 이어서 변환한다.

브라우저의 녹화 표시는 `녹화 중 → 마무리 중 → 전송 중 (Jetson → DGX x%) → 변환 중 (n / 전체 프레임) → 검증 중 → 대기`로 바뀐다. 라벨 창은 녹화를 멈추면 바로 뜨고, 입력한 라벨은 변환이 끝나면 저장된다.

카메라 프레임 선택(`--camera-sampling`, 기본 `buffered`): Jetson 팔 루프 주기는 18~50 ms로 흔들린다(p5~p95). 주기마다 가장 새 프레임을 쓰면, 긴 주기 다음의 짧은 주기에서 같은 영상이 반복되고 한 장은 건너뛴다. `buffered`는 카메라마다 아직 쓰지 않은 가장 오래된 프레임을 쓰되 최신보다 1장 넘게 뒤처지지 않게 하고, 15주기 연속 뒤처지면 최신으로 맞춘다. 대신 영상 나이가 중간값 기준 약 10~20 ms 늘어난다. `latest`는 이전 방식이다. 프레임마다 카메라 수신 시각이 `capture_cycles.jsonl`에 남는다.

| Jetson `edge.py` 옵션 | 기본값 | 내용 |
|---|---|---|
| `--record-dir` | `~/ws/teleop_jetson/recordings` | Jetson 녹화 위치 (여유 20 GB 미만이면 녹화 거부) |
| `--rgb-crf`, `--rgb-preset` | 18, superfast | RGB mp4 화질과 CPU 사용량 |
| `--camera-sampling` | buffered | 위 설명 |
| `--preview-hz-recording` | 30 | 녹화 중 실시간 RGB 화면 목표 Hz (writer가 밀리면 10 Hz로 자동 조절) |
| `--preview-depth-hz` | 10 | DGX로 보내는 깊이 화면 Hz (Jetson 로컬 녹화와 무관) |
| `--upload-mbps` | 200 | 세션 업로드 속도 제한 |

### 장비별 코드 위치와 배포

| | DGX Spark (`192.168.0.104`) | Jetson (`192.168.0.22`, `ssh midbar@192.168.0.22`) |
|---|---|---|
| 역할 | 웹서버 8081, 실시간 화면, 세션 수신·변환·검증 | 센서, 팔 추종 제어, 베이스, 녹화, 송신 (웹서버 없음) |
| 코드 | 프로젝트: `~/ws/teleop_server` | 소스: `~/ws/teleop_server` (Git 동기화 대상)<br>실행본: `~/ws/teleop_server/edge_runtime/teleop_server` (실제로 쓰는 18개 파일) |
| 서비스 | `teleop-dgx` (`distributed/deploy/run_dgx.sh`) | `teleop-jetson` (`run_edge.sh`), `teleop-jetson-slate` (`run_slate.sh`) |
| 실행 환경 | `lerobot_trossen/.venv`, 이 프로젝트의 `edge.env` (토큰) | `~/ws/teleop_jetson/.venv`, `slate_ws`, `packages/` (팔 드라이버), `edge.env` (토큰) |

실행본에 들어가는 파일은 `distributed/deploy/jetson_runtime_files.txt`에 적혀 있다. 코드를 바꾸면 이렇게 배포한다.

1. Jetson의 `~/ws/teleop_server` 소스를 Git으로 업데이트한다.
2. Jetson에서 `bash ~/ws/teleop_server/teleop_server/distributed/deploy/build_jetson_runtime.sh`를 실행해 실행본을 다시 만든다.
3. Jetson에서 `systemctl --user daemon-reload && systemctl --user restart teleop-jetson`을 실행한다.

edge가 새 모듈을 import하게 되면 목록에도 추가해야 한다. 빠진 파일이 있으면 실행본에서 시작할 때 `ModuleNotFoundError`가 난다.

### 원본 depth 저장

Jetson은 depth를 color에 정렬하지 않고 센서의 z16 영상을 그대로 저장한다(`edge.py --depth-mode raw`, 기본값). DGX는 값을 바꾸지 않고 PNG로 옮긴다. LeRobot depth 비디오는 12-bit로 양자화되므로 쓰지 않는다.

```
dataset/<session>/          세션 이름 = Jetson 녹화 시작 시각 (KST)
  dataset/                          LeRobot: RGB 3대(H.264), 팔, 베이스, observation.camera_depth_dt_ms,
                                    observation.capture_sequence (Jetson 팔 주기 번호, 빈 번호 = 기록 안 된 주기)
  depth_raw/camera1/000000.png      uint16 PNG, 파일 번호 = 데이터셋 frame_index
  depth_raw/camera2/...  camera3/...
  rgb_mp4/camera1.mp4 ...           Jetson이 녹화한 RGB 원본 (데이터셋 비디오는 LeRobot이 다시 인코딩한 것)
  capture_cycles.jsonl              프레임마다 Jetson 시각, 팔 주기 번호, 카메라 프레임 번호·수신 시각
  base_raw.jsonl                    녹화 중 받은 베이스 원본 메시지 전부
  session.json                      depth.cameras.<camera>: depth_scale_m, depth_intrinsics,
                                    color_intrinsics, depth_to_color_extrinsics; capture_gaps; source_session
```

- 미터 = PNG 값 × `depth_scale_m`(D405는 0.0001)이다. 0은 측정값 없음이다.
- `depth_to_color_extrinsics.rotation`은 librealsense처럼 열 우선(column-major) 순서다.
- RGB 픽셀과 depth 픽셀은 1:1로 대응하지 않는다. 두 영상은 같은 RealSense 프레임셋에서 나왔고, 시각 차이는 `observation.camera_depth_dt_ms`에 기록된다.
- `python -m teleop_server.validate <session>`은 카메라마다 PNG가 프레임 수만큼 있는지, uint16인지, depth 해상도와 맞는지 확인한다.
- `--depth-mode aligned`로 바꾸면 color에 정렬한 depth를 같은 형식으로 저장한다.

| 2026-10-07 측정 (카메라 2대 연결 상태) | 이전 (한 패킷씩 확인) | 이후 |
|---|---|---|
| DGX 수신 / 브라우저 RGB | 10 Hz | 30 Hz |
| 버린 주기 | 캡처의 2/3 | 0 |
| 캡처 → DGX 수신 p50 / p95 | 172 / 205 ms | 71~85 / 116~120 ms |
| Wi-Fi 사용량 | 3.9 MB/s | 11.6 MB/s (실측 TCP 한계 약 40 MB/s) |
| Jetson CPU (6코어) | 약 33% | 약 48% |

2026-10-08 Jetson 로컬 녹화, 실제 장비(카메라 3대, 팔 4대 연결·정지 자세, 베이스 정지):

| 항목 | 20초 (처음) | 30초 (depth·JPEG 낭비 제거 후) | 60초 (+ `buffered` 선택) |
|---|---|---|---|
| 저장 프레임 / 건너뛴 주기 | 426 / 153 (21 Hz) | 896 / 0 (29.7 Hz) | 1783 / 0 (29.5 Hz) |
| 같은 카메라 영상 반복 | 16% | 20~26% | 7~8.5% |
| 업로드 크기 / 시간 | 238 MB / 10초 | 약 0.5 GB / 22초 | 약 1.1 GB / 47초 |
| 변환 + 검증 | 10초 | 18초 | 37초 |
| 전송 중 제어 RTT 최대 | 15 ms | 55 ms | 31 ms |

- 21 Hz의 원인은 Jetson CPU 낭비였다. depth를 float32로 바꿨다가 다시 uint16으로 되돌리는 데 카메라당 10.2 ms가 걸렸고(지금은 원본 uint16에서 바로 2.6 ms, 결과 바이트 동일), 아무도 쓰지 않는 JPEG를 매 프레임 만들고 있었다(3.3 ms). 고친 뒤 Jetson 단독 벤치마크에서 카메라당 51~53 fps까지 쓸 수 있다.
- 남은 반복의 대부분은 해당 센서 프레임이 짝 있는 프레임으로 오지 않은 경우다. 분당 26~44개는 depth가 다른 노출과 묶여 와서 제외됐고, 나머지는 카메라 스레드가 최대 약 140 ms 멈춰 librealsense에서 프레임이 버려진 경우로 보인다.
- 저장 크기는 depth가 대부분이다. 60초 세션은 DGX에서 depth PNG 951 MB, RGB mp4 70 MB였다.

원본 depth로 바꾼 뒤(카메라 2대 연결)에는 30 Hz, 버린 주기 0, 캡처 → DGX 수신 p50 61~79 ms / p95 82~106 ms, Wi-Fi 13.0 MB/s였다. 같은 PC에서 Jetson과 DGX를 별도 프로세스로 띄우고 가짜 센서로 25초 녹화했을 때는 754프레임(30.0 Hz)이었고, 카메라마다 depth PNG가 754장이었으며 검증을 통과했다.

## 테스트

```bash
cd /home/midbar/ws/teleop_server
# 하드웨어 없이 (합성 센서 + 헤드리스 Chrome). 서비스가 켜져 있어도 된다.
/home/midbar/ws/lerobot_trossen/.venv/bin/python -m unittest discover -s teleop_server/tests -t . -p 'test_*.py'

# Jetson → DGX 분산 모드 (한 프로세스 안에서 가짜 센서로 Jetson 녹화 → 전송 → 변환 → 검증,
# 업로드 실패 재시도, 녹화 중 Wi-Fi 끊김, 프레임 선택, depth 코덱)
/home/midbar/ws/lerobot_trossen/.venv/bin/python -m unittest teleop_server.distributed.test_distributed

# 실제 장비. 먼저 systemctl --user stop teleop-server. TELEOP_ARMS=1이면 팔이 움직인다.
source /opt/ros/jazzy/setup.bash; export ROS_LOG_DIR=/tmp/lerobot-ros-logs
TELEOP_HW=1 TELEOP_ROS=1 TELEOP_ARMS=1 /home/midbar/ws/lerobot_trossen/.venv/bin/python -m unittest discover -s teleop_server/tests -t . -p 'test_*.py'
```

| 파일 | 내용 |
|---|---|
| `test_1_cameras.py`, `test_1b_camera_recovery.py` | 카메라 수신·표시, 프레임 정지·USB 분리·짝 상실 후 자동 복구 |
| `test_2_arms.py`, `test_6_control.py` | 팔 스트림·추종·종료 순서, 버튼 제어 시작·중지·재시작 |
| `test_3_odom.py` | odometry 수신, 브라우저 표시 |
| `test_4_latency.py` | 통계, 시계 오차 보정, 수신 없음 경고, 느린 클라이언트 |
| `test_5_full_system.py` | 실제 장비 제어·depth 녹화·검증·중지, 단계별 카메라 끊김 |
| `test_7_record.py`, `test_8_buttons.py` | 기존 형식 일치, 검증, 색 순서, 예외 상황, 브라우저 버튼 |
| `test_9_depth.py` | depth 저장, 프레임 단위·픽셀 단위 짝, 불일치 묶음 제외, 실제 카메라 짝 |
| `test_10_labels.py` | 라벨 API 검증·저장·수정, 브라우저 라벨 입력 |
| `test_11_base.py` | 켜기 전 거부, 속도 제한, 한 명 조작, 감시, 해제·연결 끊김 정지, 조이스틱·키보드, ROS 발행 |
| `test_12_layout.py` | 카메라 끌어서 이동·크기 조절·키보드, 새로고침 후 유지, 미리 설정, 좁은 화면 |

## 2026-10-07 실제 장비 측정

| 항목 | 결과 |
|---|---|
| 카메라 3대 RGB+depth | 각 30 fps, RGB와 depth 시각 차이 0.305 ms |
| 10초 녹화 (테스트) | 299프레임, depth 카메라당 299프레임, 검증 통과 |
| 서비스로 5초 녹화 | 150프레임, 검증 통과, 반복 프레임 0, 불일치 제외 0 |
| 녹화 중 카메라 최대 나이 | 26~34 ms |
| 팔 루프 주기 초과 | 833주기 중 2회 |
| 가상 컨트롤러 ROS 발행 | 테스트 토픽에서 20.0 Hz, 놓으면 마지막 명령 0 |

## 알려진 문제

- Jetson 로컬 녹화는 멈춘 뒤 세션이 DGX에 도착해 변환될 때까지 녹화 시간의 약 1.4배를 기다린다(60초 녹화에 87초). 그동안 다음 녹화는 시작할 수 없다.
- Jetson 로컬 녹화의 카메라 영상 반복이 7~8.5% 남아 있다(위 측정 참고).

- depth를 함께 녹화하면 녹화 중 실시간 화면의 카메라 프레임 간격이 최대 약 120 ms까지 늘어난다. 저장되는 데이터의 카메라 나이는 34 ms 이하였다.
- 녹화를 멈춘 뒤 인코딩하는 동안 실시간 화면이 최대 약 0.4초 멈춘다. 긴 녹화는 저장에 수 분 걸릴 수 있다.
- 2026-10-07 18:00경 서비스를 처음 시작했을 때 RGB와 depth 짝 불일치가 11~37%, 카메라가 18~27 fps로 떨어졌다. 서비스를 다시 시작하자 사라졌고 원인은 확인하지 못했다. 이제 짝 불일치가 10%를 넘으면 녹화를 막고, 20%를 넘는 상태가 이어지면 그 카메라를 자동으로 다시 연다.
- 팔 컨트롤러 CAN 오류처럼 프로세스가 강제 종료되면 진행 중인 에피소드는 저장되지 않는다. 서비스는 자동으로 다시 시작되고, 팔은 버튼을 누를 때까지 연결되지 않는다.
- 2026-10-06 21:14경 카메라와 SLATE USB 시리얼이 함께 USB에서 사라져 전원을 다시 켰다. 원인은 확인하지 못했다.
- 첫 통합 실행 두 번에서 왼쪽 팔로워 컨트롤러가 연결 설정 중 TCP를 끊었다. 원인은 확인하지 못했다.

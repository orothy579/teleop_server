# Jetson 수집부 이전 사전 점검

점검일: 2026-10-07 (Asia/Seoul)

## 작업 범위와 현재 상태

- DGX의 `/home/midbar/ws/lerobot_trossen/teleop_web`은 수정하지 않았다.
- `teleop_web_baseline.sha256.json`에 원본 소스 38개 파일의 SHA-256을 기록했고, 점검 종료 시 일치했다. `__pycache__`는 제외했다.
- 이 작업에서 Jetson에 수행한 명령은 조회뿐이다. 드라이버 설치, 서비스 실행, 로봇 제어 명령은 수행하지 않았다.
- 첫 SSH 접속 이후 다른 작업에 의해 Jetson의 Wi-Fi IP가 `.117`에서 `.22`로 변경되었다. 원격 호스트에 별도 Codex 프로세스가 실행 중이며, `~/ws/teleop_web`과 `~/ws/teleop_distributed/network`가 새로 생긴 것을 확인했다. 동시 변경 여부에 대한 사용자 확인을 요청했다.
- 분산 수집/전송 서버 구현 및 실제 데이터 검증은 아직 수행하지 않았다.

## 확인된 구성

| 항목 | 확인 결과 |
|---|---|
| DGX Spark Wi-Fi | `192.168.0.104/24` |
| Jetson 모델 | NVIDIA Jetson Orin Nano Engineering Reference Developer Kit Super |
| OS / 커널 | Ubuntu 24.04.4 / `6.8.12-1021-tegra`, aarch64 |
| Jetson Wi-Fi | `wlP1p1s0`, `COCELO_5G` |
| 고정 IP | `192.168.0.22/24`, `ipv4.method=manual`, 자동 연결 켜짐 |
| 게이트웨이 / DNS | `192.168.0.1` / `168.126.63.1` |
| 로봇 유선망 | `enP8p1s0`, `192.168.1.10/24` |
| 대체 SSH 경로 | `midbar@100.102.12.17` (Tailscale), 로그인 확인 |
| 시간 동기화 | `NTPSynchronized=yes`, `Asia/Seoul` |
| 카메라 | Intel RealSense D405 3대 USB 열거 확인 |
| 베이스 USB | CH340, `1a86:7523`, USB 일련번호 `chassis` |
| 로봇 ping | `.2`, `.3`, `.4` 응답. `.5`는 단일 시도에서 미응답 |

## 수집 전 해결할 항목

1. 카메라 3대가 모두 같은 USB 2.0 480M 상위 경로에 연결되어 있다. 기존 640×480 RGB+depth 30 fps 동시 수집 목표를 위해 케이블·허브의 USB 3 연결부터 점검해야 한다. USB 열거만 확인했으며 영상 수집은 시험하지 않았다.
2. CH340의 드라이버가 `[none]`이고 `modinfo ch341`은 모듈을 찾지 못했다. `/dev/ttyUSB*`도 보이지 않는다. 현재 커널 헤더는 존재한다. 커널에 맞는 드라이버 설치와 시리얼 접근 권한 확인이 필요하다.
3. 시스템 Python 3.12에 `numpy`, `cv2`는 있으나 `aiohttp`, `pyrealsense2`, `rclpy`, `trossen_arm`은 없다. `/opt/ros` 설치도 확인되지 않았다. 별도 가상환경 및 필요한 ROS/센서 드라이버 구성이 필요하다.
4. 기존 설정상 왼쪽 팔로워인 `192.168.1.5`의 전원·배선을 확인하고 재검증해야 한다. ping 한 번의 미응답만으로 장치 고장을 판단할 수는 없다.

## 원본에서 확인한 데이터와 이전 방향

Jetson에서 카메라 RGB와 같은 노출의 RGB 격자 정렬 depth, 렌즈 파라미터, 프레임 번호·시각·짝 검증 정보를 수집한다. 팔 4대의 위치·속도·effort·컨트롤러 시각과 팔로워 명령, 베이스 odometry 13개 값 및 `/cmd_vel` 6개 값과 원본 ROS 메시지도 전송 대상이다.

DGX는 별도 서버에서 데이터를 수신하여 브라우저 스트리밍 및 기존 형식에 맞는 데이터셋 저장을 담당하는 구성이 적합하다. 기존 `teleop_web` 서버와 소스를 변경하지 않고 독립된 경로·포트·서비스를 사용한다. 단순히 기존 브라우저 WebSocket만 중계하면 depth 미리보기만 전달되므로 실제 depth 데이터와 기록용 원본 RGB 전송 경로가 추가로 필요하다.

팔의 리더→팔로워 추종과 베이스 명령 타임아웃은 Jetson에서 처리해야 한다. 두 호스트의 monotonic 시각을 직접 빼지 않고 수집 시각과 전송/수신 시각을 구분한다. 연결 단절, 역압, 프레임 누락, RGB/depth 짝 유지, 저장 결과를 합성 데이터로 먼저 검증한 뒤 실장비에서 확인한다.

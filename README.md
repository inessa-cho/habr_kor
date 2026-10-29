# Хабр Kor — MADLAD + GPT-4.1 mini

A personal web app that translates Russian Habr articles into Korean with MADLAD and GPT post-editing.

Habr 러시아어 게시글을 한국어로 번역해 보관하는 개인용 PWA입니다.

## 파이프라인

1. Habr HTML 다운로드 및 러시아어 텍스트 노드 추출
2. MADLAD-400 3B로 러시아어→한국어 초벌 번역
3. 러시아어 원문과 MADLAD 초벌 번역을 함께 GPT-4.1 mini에 전달
4. GPT-4.1 mini가 오역·누락·러시아어식 어순과 깨진 문장 연결을 교정
5. 최종 텍스트를 원래 HTML 구조에 복원

GPT가 처음부터 전체 번역을 생성하는 방식이 아니라, MADLAD 초벌 번역을 원문과 대조해 고치는 방식입니다.

## OpenAI 설정

```bash
cp .env.example .env
nano .env
```

`.env`의 다음 값을 실제 API 키로 교체합니다.

```dotenv
OPENAI_API_KEY=sk-...
```

API 키는 브라우저에 노출되지 않고 백엔드 컨테이너에서만 사용됩니다. `.env`는 Git에 커밋하지 마세요.

## 실행

```bash
docker compose down
docker compose up -d --build
```

기본 주소:

```text
http://서버주소:8010
```

로그:

```bash
docker compose logs -f --tail=100 habr-translator
```

## API 후편집 안정화

- 기본 8개 텍스트 노드를 한 요청으로 처리
- OpenAI Structured Outputs의 strict JSON schema 사용
- 입력 ID와 출력 ID 및 개수 검증
- 요청 실패 시 최대 3회 재시도
- 큰 배치가 계속 실패하면 절반씩 재귀 분할
- 단일 노드만 끝까지 실패하면 그 노드만 MADLAD 결과로 fallback
- SQLite에 MADLAD와 GPT 진행률을 별도로 저장
- 서버 재시작 후 미완료 단계부터 재개

## 기존 v7 DB 업데이트

기존 DB와 MADLAD 모델을 그대로 사용할 수 있습니다.

- 기존 완료 문서는 유료 API로 자동 재처리하지 않습니다.
- 업데이트 후 새로 등록하는 문서부터 GPT-4.1 mini 후편집이 적용됩니다.
- 진행 중이던 문서는 저장된 MADLAD 작업을 이어서 완료한 뒤 API 후편집으로 넘어갑니다.

기존 완료 문서를 새 파이프라인으로 다시 번역하려면 해당 작업 데이터를 삭제한 뒤 URL을 다시 등록해야 합니다.

## 주요 환경 변수

- `OPENAI_API_KEY`: 필수
- `OPENAI_MODEL`: 기본 `gpt-4.1-mini`
- `OPENAI_BATCH_SIZE`: 기본 `8`
- `OPENAI_MAX_RETRIES`: 기본 `3`
- `OPENAI_TIMEOUT_SECONDS`: 기본 `120`
- `APP_PORT`: 기본 `8010`
- `MADLAD_CPU_THREADS`: 기본 `3`

## 비용 제어

API에는 러시아어 원문과 MADLAD 한국어 초안이 모두 입력되므로 입력 토큰이 발생하고, 최종 한국어가 출력 토큰으로 과금됩니다. 비용은 글 길이와 모델의 실제 토큰화에 따라 달라집니다. OpenAI 대시보드에서 프로젝트별 월 예산 및 알림을 함께 설정하는 것을 권장합니다.


## 권장 번역 시간 안내

브라우저 현지 시간 기준으로 오후 11시부터 오전 8시 이전까지를 권장 번역 시간으로 간주합니다.
이 시간대 밖에서 `저장 및 번역`을 누르면 다음 확인 팝업이 표시됩니다.

> 번역 배치 작업은 오후 11시 이후부터 오전 8시 이전까지 하는 것이 권장됩니다. 지금 번역을 시작하시겠습니까?

취소하면 URL은 등록되지 않으며, 확인하면 기존과 동일하게 즉시 번역을 시작합니다.


## Implementation notes

The active worker uses MADLAD followed by openai_editor.refine_batch. The separate older polisher module is retained as an earlier implementation, not presented as the active workflow. Source HTML, translations, SQLite data, model weights and real API keys are excluded. No paid API request or translation job was run.

## Content and models

No Habr article bodies, translations, downloaded article images, database or model weights are included. Fetching or sharing articles and translations requires permission under the applicable content terms; publishing this application does not grant rights to that content.

See [PRESERVATION.md](PRESERVATION.md) for the maintenance record, excluded files and verification scope.

## Complete 화면 맞춤

Complete의 각 작업 박스에 있는 **화면 맞춤** 버튼은 기존 번역 HTML을 별도 읽기 화면으로 엽니다. 박스 안에 번역 본문을 삽입하지 않습니다. 자체 CSS로 어두운 회색 배경과 밝은 글자, 읽기 좋은 본문 폭을 적용하며 외부 Habr CSS에 의존하지 않습니다. 가로 화면에서는 이미지를 본문보다 조금 작게, 세로 화면에서는 추가 축소 없이 표시하고 이미지 비율과 창 크기 변경에 자동 대응합니다.

적용 표시는 작업별로 현재 브라우저의 localStorage에 저장되어 새로고침 후 유지됩니다. 재클릭해도 해제되지 않으며, 저장소가 차단된 경우 현재 탭에서만 표시가 유지됩니다. 다른 작업 박스, 기존 원본·번역본 링크, 저장된 HTML 및 번역 파이프라인은 변경하지 않습니다.

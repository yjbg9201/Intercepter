"""偽 USI エンジン（子プロセス）。`python -m takeover.fakes.fake_engine [引数]` で起動する。

担当: M0（完了条件: usi / isready / go / stop / quit に応答する）
参照: SPEC §9「偽エンジン」

- cshogi でランダムな合法手を返す。合法手がなければ `bestmove resign`。
- 標準入力を 1 行ずつ読み、標準出力へ返す。ここは別プロセスなので I3 の対象外
  （思考中に stop を受け取るためにスレッドや select を使ってよい）。
- 引数で切り替える故障・挙動（SPEC §9 の一覧）:
  --seed N / --think-ms N / --ponder / --infinite / --multipv N /
  --stop-delay-ms N / --ignore-stop / --crash-rate P / --crash-on-go N / --crlf /
  --illegal-move / --resign / --win / --rogue-ponder-bestmove / --no-usiok / --no-readyok /
  --huge-info BYTES
- 決定的であること（同じ seed・同じ入力なら同じ出力）。レースの再現に使う。
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fake_engine", description="テスト用の偽 USI エンジン")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--think-ms", type=int, default=0)
    # 残りの引数は M0 で追加する（上の docstring の一覧）
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    print("fake_engine: 未実装（M0）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

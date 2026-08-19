"""
V6.2.3 LLM Sentiment Provider —— LLM财经情绪分析接口（预留）
==========================================================

基于论文三的核心启示：
- 论文三证明了财经新闻情绪对收益率有预测力
- 但在2026年，Word2Vec+情感词典的方法已被LLM完全替代
- V6.2.3 只定义接口，不做实现
- 未来通过 Qwen/DeepSeek API 接入

架构位置（V6.5）：
    NewsSentimentProvider (抽象接口)
            ↓
    ┌───────┴───────┐
    │               │
    Qwen API    DeepSeek API

数据流：
    新华社/财联社/人民日报
            ↓
    Qwen / DeepSeek API
            ↓
    今日市场情绪：0-100（结构化输出）
            ↓
    EmotionBuilder融合

当前状态：预留接口，不做实现。
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, Optional, List


class NewsSentimentProvider(ABC):
    """
    财经新闻情绪提供者（抽象基类）

    定义了LLM情绪分析的接口规范。
    V6.5之前为桩实现，不产生实际调用。
    """

    @abstractmethod
    def get_daily_sentiment(self, date: datetime) -> Dict:
        """
        获取指定日期的市场情绪

        Args:
            date: 日期

        Returns:
            {
                'date': str,
                'sentiment_score': float (0-100, 越高越乐观),
                'confidence': float (0-1, LLM对判断的自信度),
                'summary': str (一句话情绪摘要),
                'keywords': list[str] (关键主题词),
                'source': str ('qwen' / 'deepseek'),
                'success': bool,
                'error': str or None,
            }
        """
        pass

    @abstractmethod
    def get_sentiment_timeseries(self, start_date: datetime, end_date: datetime) -> List[Dict]:
        """
        获取日期范围内的情绪时间序列

        Args:
            start_date: 开始日期
            end_date: 结束日期

        Returns:
            list of sentiment dicts
        """
        pass


class MockNewsSentimentProvider(NewsSentimentProvider):
    """
    桩实现：不调用任何API，始终返回中性情绪。

    用于V6.2.3阶段，当LLM接口不可用时的回退。
    """

    def __init__(self):
        self.name = "MockNewsSentiment"

    def get_daily_sentiment(self, date: datetime) -> Dict:
        return {
            'date': date.strftime('%Y-%m-%d') if hasattr(date, 'strftime') else str(date),
            'sentiment_score': 50.0,
            'confidence': 0.0,
            'summary': 'LLM未接入，返回中性',
            'keywords': [],
            'source': 'mock',
            'success': True,
            'error': None,
        }

    def get_sentiment_timeseries(self, start_date: datetime, end_date: datetime) -> List[Dict]:
        return []


class LLMSentimentProvider(NewsSentimentProvider):
    """
    LLM情绪分析（V6.5预留实现框架）

    未来通过 OpenAI-compatible API 调用 Qwen/DeepSeek。
    当前不实现具体API调用逻辑。
    """

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None,
                 model: str = "qwen-turbo"):
        """
        Args:
            api_key: API密钥
            base_url: API基础URL
            model: 模型名称
        """
        self.api_key = api_key
        self.base_url = base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        self.model = model
        self._enabled = api_key is not None

    def get_daily_sentiment(self, date: datetime) -> Dict:
        """V6.5待实现"""
        return {
            'date': date.strftime('%Y-%m-%d'),
            'sentiment_score': 50.0,
            'confidence': 0.0,
            'summary': 'LLM未实现',
            'keywords': [],
            'source': 'placeholder',
            'success': False,
            'error': 'LLMSentimentProvider not yet implemented (V6.5)',
        }

    def get_sentiment_timeseries(self, start_date: datetime, end_date: datetime) -> List[Dict]:
        return []

    def is_available(self) -> bool:
        return self._enabled


# ============================================================
# Prompt 模板（预定义，未来LLM调用时使用）
# ============================================================

SENTIMENT_PROMPT_TEMPLATE = """
你是一个专业的金融市场分析师。请分析以下A股市场数据，输出今日市场情绪评估。

## 市场数据
- 上证指数变化: {sh_index_change}
- 科创综指 ETF变化: {a500_change}
- 成交量: {volume}
- 涨跌家数比: {advance_decline}
- 北向资金: {north_bound}

## 财经新闻摘要
{news_summary}

## 输出格式（严格JSON）
{{
    "sentiment_score": <0-100的整数，0=极度恐慌，50=中性，100=极度乐观>,
    "confidence": <0-1的小数，表示判断的把握>,
    "summary": "<一句话情绪概括，不超过30字>",
    "keywords": ["关键词1", "关键词2", "关键词3"],
    "reasoning": "<简要分析理由，不超过100字>"
}}
"""


def create_sentiment_provider(provider_type='mock', **kwargs):
    """
    工厂函数：创建情绪提供者实例

    Args:
        provider_type: 'mock' | 'llm'
        **kwargs: 传递给具体实现的参数

    Returns:
        NewsSentimentProvider 实例
    """
    if provider_type == 'llm':
        return LLMSentimentProvider(**kwargs)
    else:
        return MockNewsSentimentProvider()


if __name__ == "__main__":
    provider = create_sentiment_provider('mock')
    result = provider.get_daily_sentiment(datetime(2026, 7, 21))
    print(f"Mock News Sentiment: {result}")

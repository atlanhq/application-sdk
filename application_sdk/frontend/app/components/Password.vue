<script setup lang="ts">
import { Input } from '@atlanhq/atlantis'
import type { PasswordWidget } from '~/types/workflows'

const { property } = defineProps<{
    property: PasswordWidget & { id: string; ui: Record<string, unknown> }
    disabled?: boolean
}>()

const modelValue = defineModel<string>()

const componentProps = computed(() => property.ui)
</script> 

<template>
        <Input
            :id="property.id"
            v-model="modelValue"
            variant="default"
            state="idle"
            v-bind="componentProps"
            :disabled="disabled"
            type="password"
        />
</template>

